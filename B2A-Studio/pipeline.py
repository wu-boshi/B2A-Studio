"""Chapter-by-chapter novel parsing pipeline for B2A-Studio Phase 2."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import Counter
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable

import requests

from db import (
    PERSONALITY_MAX_CHARS,
    ROLLING_RANK_TOP_N,
    clear_blocked_segments_for_chapter,
    clear_checkpoints,
    clear_chunk_checkpoint,
    ensure_database,
    get_completed_chunk_indices,
    get_connection,
    get_importance_stats,
    get_pipeline_stats,
    insert_script_lines,
    is_chunk_checkpoint_valid,
    list_characters,
    mark_chunk_completed,
    refresh_rolling_character_ranks,
    replace_blocked_segments_for_chapter,
    reset_database,
    upsert_character,
)
from utils.pipeline_log import LOG_DIR, PipelineLog
from utils.prompts import (
    SCRIPT_BLOCK_FORMAT_HINT,
    SCRIPT_JSON_SCHEMA_HINT,
    SOP_SYSTEM_PROMPT,
)
from utils.script_parse import parse_script_output, salvage_script_json_text

# 解析异常时保存 content/raw 到 logs/debug/（设 B2A_DEBUG_LLM=0 可关闭）
DEBUG_LLM_RESPONSE = os.environ.get("B2A_DEBUG_LLM", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
_PARSED_LINE_ARRAY_KEYS = (
    "parsed_lines",
    "lines",
    "script_lines",
    "script",
    "rows",
    "items",
    "narration",
    "剧本",
)

STEP_API_BASE = "https://api.stepfun.com/step_plan/v1"
STEP_CHAT_URL = f"{STEP_API_BASE}/chat/completions"
STEP_MESSAGES_URL = f"{STEP_API_BASE}/messages"
# step-router 官方示例以 Chat Completions 为主；messages 见 B2A_STEP_API=messages
STEP_API_MODE = os.environ.get("B2A_STEP_API", "chat").strip().lower()
_PIPELINE_MODEL_ENV = (
    os.environ.get("B2A_LLM_MODEL", "step-router-v1").strip() or "step-router-v1"
)
LLM_MODEL = _PIPELINE_MODEL_ENV
# 仅用于解析阶段补全被截断的 JSON，不发给 API（assistant 预填会触发 structured_outputs 400）
JSON_ASSISTANT_PREFILL = '{"characters_delta":'
# 0=不提前中止 thinking，等待思考结束后 content/text（需足够 max_tokens）
THINKING_ABORT_CHARS = int(os.environ.get("B2A_THINKING_ABORT", "0"))
# Step Plan 文档：step-router-v1 输出 max_tokens 上限约 384000（上下文很长）
ROUTER_MAX_TOKENS_CAP = 384_000
# 默认不向 API 传 max_tokens，由模型/平台自行决定；设 B2A_ROUTER_MAX_TOKENS=数字 可显式限制
OMIT_MAX_TOKENS_BY_DEFAULT = os.environ.get("B2A_OMIT_MAX_TOKENS", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)

# 遗留配置（按章模式下不再使用滑动切片）
CHUNK_SIZE = int(os.environ.get("B2A_CHUNK_SIZE", "1800"))
CHUNK_OVERLAP = int(os.environ.get("B2A_CHUNK_OVERLAP", "300"))
CHAPTER_WARN_CHARS = int(os.environ.get("B2A_CHAPTER_WARN_CHARS", "80000"))
# 无「第N章」标记时的默认切分（虚拟章）
FALLBACK_CHAPTER_SIZE = int(os.environ.get("B2A_FALLBACK_CHAPTER_SIZE", "5000"))
FALLBACK_CHAPTER_OVERLAP = int(os.environ.get("B2A_FALLBACK_CHAPTER_OVERLAP", "500"))
LLM_MAX_TOKENS = int(os.environ.get("B2A_MAX_TOKENS", "16384"))
def resolve_max_tokens(explicit: int | None = None) -> int | None:
    """None 表示请求体省略 max_tokens，使用模型默认值。"""
    if explicit is not None:
        return explicit if explicit > 0 else None
    env = os.environ.get("B2A_ROUTER_MAX_TOKENS", "").strip()
    if env and env.lower() not in ("0", "none", "omit", ""):
        return min(ROUTER_MAX_TOKENS_CAP, int(env))
    if OMIT_MAX_TOKENS_BY_DEFAULT:
        return None
    return min(ROUTER_MAX_TOKENS_CAP, 200_000)


ROUTER_MAX_TOKENS = resolve_max_tokens() or 0
STEP_OPEN_API_BASE = os.environ.get(
    "B2A_STEP_OPEN_API_BASE", "https://api.stepfun.com/v1"
).rstrip("/")
STEP_OPEN_CHAT_URL = f"{STEP_OPEN_API_BASE}/chat/completions"
OPEN_FALLBACK_MODEL = (
    os.environ.get("B2A_FALLBACK_MODEL", "step-3.5-flash").strip() or "step-3.5-flash"
)
ENABLE_OPEN_FALLBACK = os.environ.get("B2A_OPEN_FALLBACK", "0").strip() == "1"
LLM_TEMPERATURE = 0.2


class ThinkingOverflowError(ValueError):
    """模型长时间只输出 thinking/reasoning，未见 JSON 正文。"""


def effective_pipeline_model() -> str:
    return LLM_MODEL


def pipeline_uses_step_plan_router() -> bool:
    return LLM_MODEL.startswith("step-router")


def _apply_assistant_prefill(raw: str) -> str:
    text = raw or ""
    stripped = text.lstrip()
    if not stripped:
        return text
    if stripped.startswith("###B2A###") or stripped.startswith(JSON_ASSISTANT_PREFILL):
        return text
    if stripped.startswith("{"):
        return text
    return JSON_ASSISTANT_PREFILL + text


def _thinking_looks_like_json_output(thinking: str) -> bool:
    """reasoning 尾部是否已出现 B2A/剧本 JSON，而非普通分析里的花括号。"""
    tail = thinking[-8000:]
    if "###B2A###" in tail or "content<<<" in tail:
        return True
    if "parsed_lines" in tail or '"parsed_lines"' in tail:
        return True
    if '"characters_delta"' in tail or "'characters_delta'" in tail:
        return True
    if re.search(r'\{[\s\n]*"(?:parsed_lines|characters_delta)"', tail):
        return True
    return False


def _thinking_should_abort(
    thinking: str,
    text_len: int,
    *,
    limit: int | None = None,
    elapsed_sec: float = 0.0,
) -> bool:
    if text_len > 0:
        return False
    if _thinking_looks_like_json_output(thinking):
        return False

    cap = THINKING_ABORT_CHARS if limit is None else limit
    if cap > 0 and len(thinking) >= cap:
        return True

    # 章节流式：长时间只有 reasoning、未见 content 时提前中止并重试直出 JSON
    min_chars = CHAPTER_THINKING_ABORT_MIN_CHARS
    time_cap = CHAPTER_THINKING_ABORT_TIME_SEC
    if (
        limit is not None
        and min_chars > 0
        and time_cap > 0
        and len(thinking) >= min_chars
        and elapsed_sec >= time_cap
    ):
        return True

    return False


LLM_CONNECT_TIMEOUT_SEC = 15
LLM_READ_TIMEOUT_SEC = 600
# 单章流式请求：连续无新数据超过该秒数则判定超时（非全书总时长）
CHAPTER_READ_TIMEOUT_SEC = int(
    os.environ.get("B2A_CHAPTER_READ_TIMEOUT", "480")
)
STREAM_HEARTBEAT_SEC = 15
def resolve_chapter_max_tokens() -> int | None:
    env = os.environ.get("B2A_CHAPTER_MAX_TOKENS", "").strip()
    if env and env.lower() not in ("0", "none", "omit", ""):
        return min(ROUTER_MAX_TOKENS_CAP, int(env))
    return resolve_max_tokens()


CHAPTER_MAX_TOKENS = resolve_chapter_max_tokens()
# 单章 reasoning 超阈值仍无 content 则中止并重试直出 JSON（设 0 关闭）
CHAPTER_THINKING_ABORT_CHARS = int(
    os.environ.get("B2A_CHAPTER_THINKING_ABORT", "28000")
)
CHAPTER_THINKING_ABORT_MIN_CHARS = int(
    os.environ.get("B2A_CHAPTER_THINKING_ABORT_MIN", "15000")
)
CHAPTER_THINKING_ABORT_TIME_SEC = int(
    os.environ.get("B2A_CHAPTER_THINKING_ABORT_SEC", "120")
)
# 超过此字数的一章才拆成多段 API；默认整章处理（可用 B2A_CHAPTER_SINGLE_SHOT_MAX 调低）
CHAPTER_SINGLE_SHOT_MAX = int(
    os.environ.get("B2A_CHAPTER_SINGLE_SHOT_MAX", "12000")
)
CHAPTER_SUB_SLICE_SIZE = int(os.environ.get("B2A_CHAPTER_SUB_SLICE_SIZE", "5000"))
CHAPTER_SUB_OVERLAP = int(os.environ.get("B2A_CHAPTER_SUB_OVERLAP", "400"))

CHAPTER_DIRECT_JSON_SUFFIX = (
    "\n\n【输出约束】禁止长段思考或情节分析。"
    "请直接输出 B2A 块格式（###B2A### 开头，###END### 结尾），"
    "含 [line] 与 [character] 块；content 用 content<<< 与 >>> 包裹原文。"
)
LLM_MAX_RETRIES = 4

CHAPTER_PATTERN = re.compile(
    r"第\s*([0-9一二三四五六七八九十百千万零〇两]+)\s*章"
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_MOJIBAKE_HINT_RE = re.compile(r"[æçèéêëìíîïòóôõöùúûüÿ]")
_CHARACTER_ROOT_KEYS = frozenset(
    {
        "name",
        "gender",
        "age",
        "personality",
        "quote_1",
        "quote_2",
        "quote_1_instruction",
        "quote_2_instruction",
    }
)
_LINE_ROOT_KEYS = frozenset(
    {"role", "emotion_instruction", "content", "is_dialogue", "text", "voice_id"}
)

ProgressCallback = Callable[[int, int, str], None]
LogCallback = Callable[[str], None]


def estimate_chunk_count(novel_text: str) -> int:
    """全书拆解步数 = 章节数（每章一次 API）。"""
    return max(1, len(slice_novel_by_chapters(novel_text)))

_CN_NUM = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "百": 100,
    "千": 1000,
    "万": 10000,
}


@dataclass
class TextChunk:
    index: int
    start: int
    end: int
    text: str
    overlap_prefix: int


@dataclass
class NovelChapter:
    """全书按「第N章」或固定字数切分后的一段原文。"""

    index: int
    chapter_num: int
    start: int
    end: int
    text: str
    overlap_prefix: int = 0
    slice_mode: str = "marker"  # marker | fixed | sub
    preamble_chars: int = 0  # 第1章内、「第一章」标记前的书名/前言字数


@dataclass
class PipelineResult:
    chunks_total: int  # 本次运行任务数（重跑 N 章时 = N，全书拆解时 = 全书章数）
    book_chapters_total: int  # 按「第N章」切分后的全书章数
    characters: int
    script_lines: int
    chapters: int  # 库内已有剧本的章数
    chunks_skipped: int = 0
    main_characters: int = 0
    extra_characters: int = 0
    pending_characters: int = 0
    reprocess_only: bool = False


@dataclass
class PipelineResumeInfo:
    novel_fingerprint: str
    chunk_config: str
    total_chunks: int
    completed_chunks: int
    next_chunk_index: int | None
    can_resume: bool


@dataclass
class LocalBookProgress:
    """当前上传小说与本地 SQLite 的对应关系及断点状态。"""

    fingerprint: str
    total_chunks: int
    completed_chunks: int
    next_chunk_index: int | None
    can_resume: bool
    is_complete: bool
    has_local_data: bool
    checkpoint_rows: int
    script_lines: int
    characters: int
    chapters_in_db: int
    script_chapter_nums: tuple[int, ...] = ()
    chapter_gap_hint: str = ""
    resume_block_reason: str = ""
    offline_script_ready: bool = False


def chunk_config_signature() -> str:
    return (
        f"mode=chapter,v2_next_marker,fb={FALLBACK_CHAPTER_SIZE},"
        f"ov={FALLBACK_CHAPTER_OVERLAP}"
    )


def compute_novel_fingerprint(novel_text: str, novel_name: str = "") -> str:
    """断点识别指纹（仅正文长度 + 哈希，与文件名无关）。"""
    _ = novel_name  # 保留参数以兼容旧调用
    digest = hashlib.sha256(novel_text.encode("utf-8", errors="ignore")).hexdigest()[
        :16
    ]
    return f"{len(novel_text)}:{digest}"


def normalize_content_fingerprint(fingerprint: str) -> str:
    """将新/旧版指纹统一为「字数:哈希」，便于比较是否为同一本书。"""
    fp = (fingerprint or "").strip()
    if not fp:
        return ""
    parts = fp.split(":")
    if len(parts) < 2:
        return fp
    digest = parts[-1]
    length = parts[-2]
    if len(digest) == 16 and length.isdigit():
        return f"{length}:{digest}"
    return fp


def describe_script_chapter_gaps(chapter_nums: list[int]) -> str:
    """说明库内已有剧本的章节号及中间缺段（如 1、2、10 则提示 3–9 未拆解）。"""
    if not chapter_nums:
        return ""
    nums = sorted({int(n) for n in chapter_nums})
    present = "、".join(f"第{n}章" for n in nums)
    gap_parts: list[str] = []
    for left, right in zip(nums, nums[1:]):
        if right - left > 1:
            gap_parts.append(f"第{left + 1}–{right - 1}章")
    if gap_parts:
        return f"库内剧本章节：{present}；**未拆解**：{'、'.join(gap_parts)}"
    return f"库内剧本章节：{present}（连续）"


def compute_novel_fingerprint_legacy(novel_text: str, novel_name: str = "") -> str:
    """旧版指纹（含文件名），用于读取历史检查点。"""
    name = (novel_name or "novel").strip() or "novel"
    digest = hashlib.sha256(novel_text.encode("utf-8", errors="ignore")).hexdigest()[
        :16
    ]
    return f"{name}:{len(novel_text)}:{digest}"


def _novel_fingerprints(novel_text: str, novel_name: str = "") -> tuple[str, str]:
    primary = compute_novel_fingerprint(novel_text, novel_name)
    legacy = compute_novel_fingerprint_legacy(novel_text, novel_name)
    return primary, legacy


def _merge_completed_chunk_indices(
    conn,
    novel_text: str,
    novel_name: str,
    chunk_config: str,
) -> set[int]:
    """合并当前指纹与旧版指纹下的已完成任务序。"""
    from db import (
        get_completed_chunk_indices,
        get_completed_chunk_indices_relaxed,
    )

    primary, legacy = _novel_fingerprints(novel_text, novel_name)
    done: set[int] = set()
    for fp in {primary, legacy}:
        done |= get_completed_chunk_indices(conn, fp, chunk_config)
    if not done:
        for fp in {primary, legacy}:
            done |= get_completed_chunk_indices_relaxed(conn, fp)
    return done


def _checkpoint_matches_upload(
    conn,
    novel_text: str,
    novel_name: str,
) -> bool:
    from db import list_checkpoint_fingerprints

    primary, legacy = _novel_fingerprints(novel_text, novel_name)
    stored = set(list_checkpoint_fingerprints(conn))
    return bool(stored & {primary, legacy})


def _resume_block_reason(
    conn,
    novel_text: str,
    novel_name: str,
    *,
    has_local: bool,
    fingerprint_matches: bool,
    done: set[int],
    next_idx: int | None,
) -> str:
    if not has_local:
        return ""
    if not novel_text.strip():
        return "请先在上方重新上传与断点一致的 .txt 小说（刷新页面后需重新选文件）。"
    if not fingerprint_matches:
        from db import list_checkpoint_fingerprints

        stored = list_checkpoint_fingerprints(conn)
        if not stored:
            return ""
        hint = "、".join(stored[:2])
        if len(stored) > 2:
            hint += "…"
        return (
            f"当前上传正文与本地断点指纹不一致（本地断点：{hint}；"
            f"当前：{compute_novel_fingerprint(novel_text, novel_name)}）。"
            "若未改正文，请重新选择**同一份** TXT；若已改过正文，请用「启动全书拆解」重跑。"
        )
    if not done:
        return ""
    if next_idx is None:
        return "按当前章节切分，全书任务已全部标记完成。"
    return ""


def is_chunk_checkpoint_valid_for_novel(
    conn,
    novel_text: str,
    novel_name: str,
    chunk_index: int,
    chunk_config: str,
    char_start: int,
    char_end: int,
) -> bool:
    """任一新/旧指纹下检查点有效即视为可跳过。"""
    from db import is_chunk_checkpoint_valid

    primary, legacy = _novel_fingerprints(novel_text, novel_name)
    for fp in {primary, legacy}:
        if is_chunk_checkpoint_valid(
            conn,
            fp,
            chunk_index,
            chunk_config,
            char_start,
            char_end,
        ):
            return True
    return False


def get_pipeline_resume_info(
    novel_text: str,
    novel_name: str = "",
) -> PipelineResumeInfo:
    fp = compute_novel_fingerprint(novel_text, novel_name)
    cfg = chunk_config_signature()
    chapters = slice_novel_by_chapters(novel_text)
    total = len(chapters) or 1
    ensure_database()
    with get_connection() as conn:
        done = _merge_completed_chunk_indices(conn, novel_text, novel_name, cfg)
        stats = get_pipeline_stats(conn)
        fp_ok = _checkpoint_matches_upload(conn, novel_text, novel_name)
    next_idx = None
    for ch in chapters:
        if ch.index not in done:
            next_idx = ch.index
            break
    can_resume = next_idx is not None and (
        len(done) > 0 or (fp_ok and int(stats["script_lines"]) > 0)
    )
    return PipelineResumeInfo(
        novel_fingerprint=fp,
        chunk_config=cfg,
        total_chunks=total,
        completed_chunks=len(done),
        next_chunk_index=next_idx,
        can_resume=can_resume,
    )


def get_local_book_progress(
    novel_text: str,
    novel_name: str = "",
) -> LocalBookProgress:
    """
    判断本地是否已有与当前上传文件一致（指纹相同）的拆解半成品。
    can_resume=True 时可直接使用「断点续跑」，无需从头清空库。
    """
    from db import list_script_chapters

    resume = get_pipeline_resume_info(novel_text, novel_name)
    ensure_database()
    with get_connection() as conn:
        stats = get_pipeline_stats(conn)
        script_chapters = list_script_chapters(conn)
        legacy_fp = compute_novel_fingerprint_legacy(novel_text, novel_name)
        fps = (resume.novel_fingerprint, legacy_fp)
        fp_ok = _checkpoint_matches_upload(conn, novel_text, novel_name)
        done = _merge_completed_chunk_indices(
            conn, novel_text, novel_name, resume.chunk_config
        )
        checkpoint_rows = int(
            conn.execute(
                f"""
                SELECT COUNT(*) FROM pipeline_checkpoints
                WHERE novel_fingerprint IN ({",".join("?" * len(fps))})
                """,
                fps,
            ).fetchone()[0]
        )
        has_local = (
            checkpoint_rows > 0
            or stats["script_lines"] > 0
            or stats["characters"] > 0
        )
        script_lines = int(stats["script_lines"])
        offline_script_ready = script_lines > 0 and checkpoint_rows == 0
        block_reason = ""
        if has_local and not resume.can_resume and not offline_script_ready:
            block_reason = _resume_block_reason(
                conn,
                novel_text,
                novel_name,
                has_local=True,
                fingerprint_matches=fp_ok,
                done=done,
                next_idx=resume.next_chunk_index,
            )
    is_complete = (
        has_local
        and (
            offline_script_ready
            or (
                checkpoint_rows > 0
                and resume.completed_chunks >= resume.total_chunks
                and not resume.can_resume
            )
        )
    )

    return LocalBookProgress(
        fingerprint=resume.novel_fingerprint,
        total_chunks=resume.total_chunks,
        completed_chunks=resume.completed_chunks,
        next_chunk_index=resume.next_chunk_index,
        can_resume=resume.can_resume,
        is_complete=is_complete,
        has_local_data=has_local,
        checkpoint_rows=checkpoint_rows,
        script_lines=int(stats["script_lines"]),
        characters=int(stats["characters"]),
        chapters_in_db=int(stats["chapters"]),
        script_chapter_nums=tuple(script_chapters),
        chapter_gap_hint=describe_script_chapter_gaps(script_chapters),
        resume_block_reason=block_reason if has_local and not resume.can_resume else "",
        offline_script_ready=offline_script_ready,
    )


def _chinese_numeral_to_int(token: str) -> int | None:
    token = token.strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    total = 0
    current = 0
    for ch in token:
        if ch.isdigit():
            current = current * 10 + int(ch)
            continue
        if ch not in _CN_NUM:
            return None
        val = _CN_NUM[ch]
        if val >= 10:
            if current == 0:
                current = 1
            total += current * val
            current = 0
        else:
            current = current * 10 + val if current else val
    return total + current


def collect_chapter_marker_positions(novel_text: str) -> dict[int, int]:
    """各「第N章」在全文中的首次出现位置（字符偏移）。"""
    positions: dict[int, int] = {}
    for match in CHAPTER_PATTERN.finditer(novel_text):
        num = _chinese_numeral_to_int(match.group(1))
        if num is None:
            continue
        if num not in positions:
            positions[num] = match.start()
    return positions


def extract_chapter_title_after_marker(novel_text: str, marker_end: int) -> str:
    """提取「第N章」标记后、同一行内的章节名（副标题）。"""
    line_end = novel_text.find("\n", marker_end)
    if line_end < 0:
        line_end = len(novel_text)
    tail = novel_text[marker_end:line_end].strip()
    tail = re.sub(r"^[\s:：\-—·\.．、\[\]【】]+", "", tail)
    tail = re.sub(r"[\s:：\-—·\.．、]+$", "", tail)
    if not tail or CHAPTER_PATTERN.match(tail):
        return ""
    if len(tail) > 48:
        tail = tail[:48].rstrip() + "…"
    return tail


def collect_all_chapter_marker_hits(novel_text: str) -> list[dict[str, Any]]:
    """文中每一次「第N章」匹配（含重复序号）。"""
    hits: list[dict[str, Any]] = []
    for match in CHAPTER_PATTERN.finditer(novel_text):
        num = _chinese_numeral_to_int(match.group(1))
        if num is None:
            continue
        start = match.start()
        hits.append(
            {
                "chapter_num": num,
                "offset": start,
                "line": novel_text.count("\n", 0, start) + 1,
                "label": match.group(0).strip(),
                "chapter_title": extract_chapter_title_after_marker(
                    novel_text, match.end()
                ),
            }
        )
    return hits


def audit_novel_chapter_integrity(novel_text: str) -> dict[str, Any]:
    """
    拆解前章节分布体检：重复标题、缺号、实际拆解顺序说明。
    仅统计「第N章」式标记；无标记时返回 fixed 模式说明。
    """
    text = novel_text or ""
    if not text.strip():
        return {
            "slice_mode": "empty",
            "ok": False,
            "issues": ["小说正文为空。"],
            "marker_hits": [],
            "duplicate_chapters": [],
            "missing_chapters": [],
            "process_sequence": [],
            "slice_plan": [],
            "marker_count": 0,
            "process_count": 0,
        }

    positions = collect_chapter_marker_positions(text)
    if not positions:
        virtual = slice_novel_by_fixed_length(text)
        return {
            "slice_mode": "fixed",
            "ok": True,
            "issues": [],
            "marker_hits": [],
            "duplicate_chapters": [],
            "missing_chapters": [],
            "process_sequence": [c.chapter_num for c in virtual],
            "slice_plan": [
                {
                    "pipeline_index": c.index,
                    "chapter_num": c.chapter_num,
                    "chars": len(c.text),
                    "note": "按字数虚拟分段",
                }
                for c in virtual
            ],
            "marker_count": 0,
            "process_count": len(virtual),
        }

    hits = collect_all_chapter_marker_hits(text)
    counts = Counter(h["chapter_num"] for h in hits)
    duplicate_chapters = sorted(n for n, c in counts.items() if c > 1)

    marker_nums = set(positions.keys())
    max_marker = max(marker_nums) if marker_nums else 1
    missing_chapters = sorted(n for n in range(2, max_marker + 1) if n not in marker_nums)

    chapters = slice_novel_by_chapters(text)
    process_sequence = [c.chapter_num for c in chapters]

    issues: list[str] = []
    for num in duplicate_chapters:
        locs = [h for h in hits if h["chapter_num"] == num]
        where = "、".join(
            f"约第 {h['line']} 行（{h['label']}）" for h in locs[:4]
        )
        extra = f" 等 {len(locs)} 处" if len(locs) > 4 else ""
        issues.append(
            f"**重复章节号**：「第 {num} 章」在文中出现 {len(locs)} 次（{where}{extra}）。"
            "拆解时**只认第一次**出现的标题，其后正文会并入该章，直到下一个更大章节号。"
        )

    if missing_chapters:
        missing_label = "、".join(f"第 {n} 章" for n in missing_chapters)
        issues.append(
            f"**缺号**：在「第 2 章」至「第 {max_marker} 章」范围内未找到 {missing_label} 的标题。"
            "流水线会按现有标题跳号（例如第 5 章后直接处理第 7 章）。"
        )

    if len(process_sequence) != len(set(process_sequence)):
        issues.append("**拆解计划异常**：同一章节号对应多个拆解任务，请检查原文。")

    first_title_by_num: dict[int, str] = {}
    for h in hits:
        num = int(h["chapter_num"])
        if num not in first_title_by_num:
            first_title_by_num[num] = (h.get("chapter_title") or "").strip()

    slice_plan = [
        {
            "pipeline_index": c.index,
            "chapter_num": c.chapter_num,
            "chapter_title": first_title_by_num.get(c.chapter_num, ""),
            "chars": len(c.text),
            "note": (
                f"含书前 {c.preamble_chars} 字"
                if c.preamble_chars > 0
                else "正文"
            ),
        }
        for c in chapters
    ]

    chapter_catalog: list[dict[str, Any]] = []
    seen_catalog: set[int] = set()
    for h in hits:
        num = int(h["chapter_num"])
        if num in seen_catalog:
            continue
        seen_catalog.add(num)
        title = (h.get("chapter_title") or "").strip()
        chapter_catalog.append(
            {
                "chapter_num": num,
                "chapter_title": title,
                "line": h["line"],
                "label": h["label"],
            }
        )

    ok = not duplicate_chapters and not missing_chapters

    return {
        "slice_mode": "marker",
        "ok": ok,
        "issues": issues,
        "marker_hits": hits,
        "chapter_catalog": chapter_catalog,
        "duplicate_chapters": duplicate_chapters,
        "missing_chapters": missing_chapters,
        "process_sequence": process_sequence,
        "slice_plan": slice_plan,
        "marker_count": len(hits),
        "unique_marker_count": len(marker_nums),
        "process_count": len(chapters),
        "max_marker": max_marker,
    }


def chapter_slice_end_offset(
    chapter_num: int,
    positions: dict[int, int],
    text_len: int,
) -> int:
    """第 chapter_num 章结束位置：第 (chapter_num+1) 章标记开始前，或全文末。"""
    for num in sorted(positions):
        if num > chapter_num:
            return positions[num]
    return text_len


def chapter_numbers_to_process(positions: dict[int, int]) -> list[int]:
    """待拆解章节序号：始终含第 1 章；其余为文中出现的 k≥2。"""
    nums = {1}
    nums.update(n for n in positions if n >= 2)
    return sorted(nums)


def build_chapter_boundaries(novel_text: str) -> list[tuple[int, int]]:
    """每章切片的 (start_offset, chapter_num)，与 slice_novel_by_chapters 一致。"""
    positions = collect_chapter_marker_positions(novel_text)
    if not positions:
        return [(0, 1)]
    text_len = len(novel_text)
    out: list[tuple[int, int]] = []
    for chapter_num in chapter_numbers_to_process(positions):
        start = 0 if chapter_num == 1 else positions[chapter_num]
        end = chapter_slice_end_offset(chapter_num, positions, text_len)
        if start < end:
            out.append((start, chapter_num))
    return out or [(0, 1)]


def chapter_at_offset(boundaries: list[tuple[int, int]], offset: int) -> int:
    chapter = boundaries[0][1]
    for pos, num in boundaries:
        if pos > offset:
            break
        chapter = num
    return chapter


def slice_novel_text(novel_text: str) -> list[TextChunk]:
    """Sliding window chunks with overlap for context continuity."""
    if not novel_text:
        return []

    chunks: list[TextChunk] = []
    step = CHUNK_SIZE - CHUNK_OVERLAP
    start = 0
    index = 1

    while start < len(novel_text):
        end = min(start + CHUNK_SIZE, len(novel_text))
        overlap_prefix = 0 if start == 0 else CHUNK_OVERLAP
        chunks.append(
            TextChunk(
                index=index,
                start=start,
                end=end,
                text=novel_text[start:end],
                overlap_prefix=overlap_prefix,
            )
        )
        if end >= len(novel_text):
            break
        start += step
        index += 1

    return chunks


def slice_novel_by_fixed_length(
    novel_text: str,
    *,
    size: int = FALLBACK_CHAPTER_SIZE,
    overlap: int = FALLBACK_CHAPTER_OVERLAP,
) -> list[NovelChapter]:
    """无章节标题时：按固定字数滑动切分，段首保留与上一段的重叠区。"""
    if not novel_text:
        return []
    if size <= 0:
        raise ValueError("FALLBACK_CHAPTER_SIZE 须为正整数")
    if overlap < 0:
        overlap = 0
    if overlap >= size:
        overlap = max(0, size // 5)

    units: list[NovelChapter] = []
    step = size - overlap
    start = 0
    while start < len(novel_text):
        end = min(start + size, len(novel_text))
        overlap_prefix = 0 if start == 0 else overlap
        units.append(
            NovelChapter(
                index=len(units) + 1,
                chapter_num=len(units) + 1,
                start=start,
                end=end,
                text=novel_text[start:end],
                overlap_prefix=overlap_prefix,
                slice_mode="fixed",
            )
        )
        if end >= len(novel_text):
            break
        start += step
    return units


def slice_chapter_for_api(
    chapter: NovelChapter,
    *,
    force: bool = False,
) -> list[NovelChapter]:
    """过长的一章拆成多段请求（带重叠），避免单次输出 token 不够。"""
    min_len = 1200 if force else CHAPTER_SINGLE_SHOT_MAX
    if len(chapter.text) <= min_len:
        return [chapter]

    size = CHAPTER_SUB_SLICE_SIZE
    overlap = CHAPTER_SUB_OVERLAP
    if overlap >= size:
        overlap = max(0, size // 5)
    step = size - overlap
    segments: list[NovelChapter] = []
    start = 0
    while start < len(chapter.text):
        end = min(start + size, len(chapter.text))
        overlap_prefix = 0 if start == 0 else overlap
        segments.append(
            NovelChapter(
                index=len(segments) + 1,
                chapter_num=chapter.chapter_num,
                start=chapter.start + start,
                end=chapter.start + end,
                text=chapter.text[start:end],
                overlap_prefix=overlap_prefix,
                slice_mode="sub",
                preamble_chars=chapter.preamble_chars if start == 0 else 0,
            )
        )
        if end >= len(chapter.text):
            break
        start += step
    return segments


def slice_novel_by_chapters(novel_text: str) -> list[NovelChapter]:
    """
    按「第N章」逻辑切分：
    - 第 1 章：全书开头 →「第二章」标记前（无第二章则至更高序号章或文末）
    - 第 k 章（k≥2）：「第k章」标记 →「第(k+1)章」标记前（无则至文末）
    - 全文无任何「第N章」：固定字数兜底（见 slice_novel_by_fixed_length）
    """
    if not novel_text:
        return []

    positions = collect_chapter_marker_positions(novel_text)
    if not positions:
        return slice_novel_by_fixed_length(novel_text)

    units: list[NovelChapter] = []
    text_len = len(novel_text)

    for chapter_num in chapter_numbers_to_process(positions):
        if chapter_num == 1:
            start = 0
        else:
            start = positions[chapter_num]
        end = chapter_slice_end_offset(chapter_num, positions, text_len)
        if start >= end:
            continue
        text = novel_text[start:end]
        if not text.strip():
            continue

        preamble_chars = 0
        if chapter_num == 1:
            first_chapter_marker = positions.get(1)
            if first_chapter_marker is not None and first_chapter_marker > start:
                preamble_chars = first_chapter_marker - start

        units.append(
            NovelChapter(
                index=len(units) + 1,
                chapter_num=chapter_num,
                start=start,
                end=end,
                text=text,
                overlap_prefix=0,
                slice_mode="marker",
                preamble_chars=preamble_chars,
            )
        )
    return units


def _repair_utf8_mojibake(text: str) -> str:
    """
    修复 UTF-8 被误按 Latin-1/ISO-8859-1 解码的典型乱码（如 æ¢ -> 梁）。
    SSE iter_lines 未指定 encoding 时 requests 常默认 ISO-8859-1。
    """
    if not text or _CJK_RE.search(text):
        return text
    if not _MOJIBAKE_HINT_RE.search(text):
        return text
    try:
        fixed = text.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text
    return fixed if _CJK_RE.search(fixed) else text


def _repair_dict_strings(obj: Any) -> Any:
    if isinstance(obj, str):
        return _repair_utf8_mojibake(obj)
    if isinstance(obj, list):
        return [_repair_dict_strings(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _repair_dict_strings(v) for k, v in obj.items()}
    return obj


def _looks_like_line_row(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    content = (row.get("content") or row.get("text") or "").strip()
    return bool(content)


def _line_content_len(row: Any) -> int:
    if isinstance(row, dict):
        return len((row.get("content") or row.get("text") or "").strip())
    if isinstance(row, str):
        return len(row.strip())
    return 0


def _coerce_line_dict(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return None
        return {
            "role": "旁白",
            "emotion_instruction": "",
            "content": text,
            "is_dialogue": "“" in text or '"' in text,
            "voice_id": "",
        }
    if isinstance(item, dict):
        return item
    return None


def _coerce_lines_array(items: Any) -> list[dict[str, Any]]:
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        coerced = _coerce_line_dict(item)
        if coerced is not None:
            out.append(coerced)
    return out


def _gather_line_rows_from_tree(obj: Any, depth: int = 0) -> list[dict[str, Any]]:
    """在嵌套 JSON 中收集所有剧本行（含 lines / script 等别名）。"""
    if depth > 12:
        return []
    rows: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        for key in _PARSED_LINE_ARRAY_KEYS:
            if key in obj:
                rows.extend(_coerce_lines_array(obj[key]))
        if _looks_like_line_row(obj) and not (obj.get("name") or "").strip():
            rows.append(obj)
        for value in obj.values():
            if isinstance(value, (dict, list)):
                rows.extend(_gather_line_rows_from_tree(value, depth + 1))
    elif isinstance(obj, list):
        if obj and all(_coerce_line_dict(x) is not None for x in obj[: min(5, len(obj))]):
            rows.extend(_coerce_lines_array(obj))
        else:
            for item in obj:
                rows.extend(_gather_line_rows_from_tree(item, depth + 1))
    return rows


def _payload_script_metrics(obj: dict[str, Any]) -> tuple[int, int]:
    """(有效行数, 行内 content 总字数)。"""
    rows = _gather_line_rows_from_tree(obj)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        item = normalize_parsed_line(row)
        if not item:
            continue
        key = item["content"]
        if key in seen:
            continue
        seen.add(key)
        normalized.append(item)
    chars = sum(len(r["content"]) for r in normalized)
    return len(normalized), chars


def _line_row_from_dict(src: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": (src.get("role") or "旁白").strip() or "旁白",
        "emotion_instruction": str(src.get("emotion_instruction") or "").strip(),
        "content": (src.get("content") or src.get("text") or "").strip(),
        "is_dialogue": bool(src.get("is_dialogue")),
        "voice_id": str(src.get("voice_id") or "").strip(),
    }


def _normalize_script_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """修复乱码；收拢误放在顶层、嵌套键或混在 characters_delta 里的剧本行。"""
    payload = _repair_dict_strings(payload)
    chars = payload.get("characters_delta")
    gathered = _gather_line_rows_from_tree(payload)
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in gathered:
        item = normalize_parsed_line(row)
        if not item:
            continue
        key = item["content"]
        if key in seen:
            continue
        seen.add(key)
        lines.append(item)

    if not isinstance(chars, list):
        chars = []
    fixed_chars: list[dict[str, Any]] = []
    for item in chars:
        if not isinstance(item, dict):
            continue
        if _looks_like_line_row(item) and not (item.get("name") or "").strip():
            content = (item.get("content") or item.get("text") or "").strip()
            if content and content not in seen:
                seen.add(content)
                lines.append(_line_row_from_dict(item))
        elif (item.get("name") or "").strip():
            fixed_chars.append(item)

    if not fixed_chars and isinstance(payload.get("name"), str) and payload.get("name"):
        fixed_chars = [{k: str(payload.get(k) or "") for k in _CHARACTER_ROOT_KEYS}]

    payload["characters_delta"] = fixed_chars
    payload["parsed_lines"] = lines
    return payload


def _score_payload(obj: dict[str, Any]) -> int:
    """优先选择行内 content 总字数多、行数多的 JSON（避免误选只有书名的短对象）。"""
    line_count, content_chars = _payload_script_metrics(obj)
    score = content_chars * 20 + line_count * 200
    delta = obj.get("characters_delta")
    if isinstance(delta, list) and len(delta) > 0:
        score += 500 + len(delta) * 10
    return score


def _iter_sse_lines(response: requests.Response):
    """SSE 按 UTF-8 字节解码，避免 requests 用 ISO-8859-1 导致中文乱码。"""
    for raw_line in response.iter_lines(decode_unicode=False):
        if not raw_line:
            continue
        yield raw_line.decode("utf-8", errors="replace")


def _yield_json_dict_candidates(obj: Any) -> Any:
    """从解析出的 JSON 根节点展开出所有 dict 候选。"""
    if isinstance(obj, dict):
        yield obj
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                yield item


def _is_script_line_fragment(obj: dict[str, Any]) -> bool:
    """单个剧本行 dict（非含 parsed_lines 的根对象）。"""
    if "parsed_lines" in obj or "characters_delta" in obj:
        return False
    return _looks_like_line_row(obj)


def _try_parse_root_json(text: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _extract_json_array_objects(text: str, key: str) -> list[dict[str, Any]]:
    """
    从可能残缺的 JSON 文本中，按数组元素逐个 raw_decode（跳过坏行）。
    用于模型在 content 里写了未转义 ASCII 引号导致整段 json.loads 失败的情况。
    """
    key_idx = text.find(f'"{key}"')
    if key_idx < 0:
        return []
    arr_start = text.find("[", key_idx)
    if arr_start < 0:
        return []
    decoder = json.JSONDecoder()
    idx = arr_start + 1
    objects: list[dict[str, Any]] = []
    while idx < len(text):
        while idx < len(text) and text[idx] in " \t\n\r,":
            idx += 1
        if idx >= len(text) or text[idx] == "]":
            break
        if text[idx] != "{":
            idx += 1
            continue
        try:
            obj, end = decoder.raw_decode(text, idx)
            if isinstance(obj, dict):
                objects.append(obj)
            idx = end
        except json.JSONDecodeError:
            next_brace = text.find("{", idx + 1)
            if next_brace < 0:
                break
            idx = next_brace
    return objects


def _salvage_script_from_text(text: str) -> dict[str, Any] | None:
    return salvage_script_json_text(text)


def _iter_json_dicts_in_text(text: str):
    """扫描文本中所有可解析的 JSON 对象（含代码块与数组内的 dict）。"""
    if "```" in text:
        for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.I):
            block = match.group(1).strip()
            try:
                obj = json.loads(block)
            except json.JSONDecodeError:
                continue
            yield from _yield_json_dict_candidates(obj)

    decoder = json.JSONDecoder()
    idx = 0
    length = len(text)
    while idx < length:
        while idx < length and text[idx] not in "{[":
            idx += 1
        if idx >= length:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        yield from _yield_json_dict_candidates(obj)
        idx = max(end, idx + 1)


def _debug_dump_llm_parse(
    sink: PipelineLog,
    *,
    label: str,
    raw: str,
    ranked: list[tuple[int, int, int, dict[str, Any]]],
    chosen: dict[str, Any],
) -> None:
    """保存 raw 与解析诊断到 logs/debug/，并在日志中摘要。"""
    if not DEBUG_LLM_RESPONSE:
        return
    debug_dir = LOG_DIR / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\-.]+", "_", label).strip("_")[:80] or "llm"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    prefix = debug_dir / f"{stamp}_{safe}"
    try:
        prefix.with_suffix(".raw.txt").write_text(raw or "", encoding="utf-8")
        chosen_json = json.dumps(chosen, ensure_ascii=False, indent=2)
        if len(chosen_json) > 2_000_000:
            chosen_json = chosen_json[:2_000_000] + "\n…(truncated)"
        prefix.with_suffix(".chosen.json").write_text(chosen_json, encoding="utf-8")
    except OSError as exc:
        sink.warn(f"无法写入调试文件: {exc}")
        return

    n_lines = len(chosen.get("parsed_lines") or [])
    n_chars = sum(_line_content_len(x) for x in (chosen.get("parsed_lines") or []))
    sink.warn(
        f"JSON 解析诊断 [{label}]：扫描到 {len(ranked)} 个对象，"
        f"选用 {n_lines} 行 / 行内 {n_chars} 字"
    )
    lines_out = []
    for i, (score, lc, cc, obj) in enumerate(ranked[:12]):
        keys = list(obj.keys())[:10]
        lines_out.append(f"  #{i}: score={score} lines={lc} chars={cc} keys={keys}")
    if lines_out:
        sink.detail("JSON 候选（按 score 排序）：\n" + "\n".join(lines_out))
    sink.detail(f"content/raw 开头：\n{_preview_text(raw, 600)}")
    raw_tail = (raw or "")[-600:]
    if len(raw or "") > 700:
        sink.detail(f"content/raw 结尾：\n{_preview_text(raw_tail, 600)}")
    sink.progress(
        f"已保存调试：logs/debug/{prefix.name}.raw.txt 与 .chosen.json"
    )


def _extract_best_json_dict(
    raw: str,
    *,
    sink: PipelineLog | None = None,
    debug_label: str = "",
) -> dict[str, Any]:
    """
    从模型输出中选取剧本 JSON。

    优先整段 json.loads；失败时从 parsed_lines 数组逐条 salvage（常见：content 内未转义引号）。
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("模型输出为空")

    ranked: list[tuple[int, int, int, dict[str, Any]]] = []
    best_obj: dict[str, Any] | None = None

    parsed_payload, parse_method = parse_script_output(text)
    if parsed_payload is not None:
        best_obj = parsed_payload
        ranked.append(
            (
                _score_payload(parsed_payload),
                *_payload_script_metrics(parsed_payload),
                parsed_payload,
            )
        )
        if sink and parse_method in ("salvage", "json_repair"):
            s_lc, s_cc = _payload_script_metrics(parsed_payload)
            sink.warn(
                f"模型输出非标准 JSON，已用 {parse_method} 解析："
                f"{s_lc} 行 / 行内 {s_cc} 字（content 引号不丢失）"
            )
        elif sink and parse_method == "block":
            sink.detail("已按 B2A 块格式解析模型输出")
    else:
        root = _try_parse_root_json(text)
        if root is not None:
            best_obj = root
            ranked.append(
                (_score_payload(root), *_payload_script_metrics(root), root)
            )

    if best_obj is None:
        fragments: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for obj in _iter_json_dicts_in_text(text):
            oid = id(obj)
            if oid in seen_ids:
                continue
            seen_ids.add(oid)
            if _is_script_line_fragment(obj):
                fragments.append(obj)
                continue
            line_count, content_chars = _payload_script_metrics(obj)
            score = _score_payload(obj)
            ranked.append((score, line_count, content_chars, obj))

        if fragments:
            frag_chars = sum(_line_content_len(x) for x in fragments)
            merged = {
                "parsed_lines": fragments,
                "characters_delta": _extract_json_array_objects(
                    text, "characters_delta"
                ),
            }
            ranked.append(
                (
                    _score_payload(merged),
                    len(fragments),
                    frag_chars,
                    merged,
                )
            )

    if not ranked:
        if "{" not in text:
            raise ValueError(
                "模型返回纯文本，未包含 JSON。"
                "请确认已等待 content 字段输出完成。"
            )
        raise ValueError(
            "无法从模型输出中解析有效 JSON（需含 parsed_lines 或同义字段）。"
        )

    ranked.sort(key=lambda t: (t[0], t[2], t[1]), reverse=True)
    _best_score, best_lc, best_cc, best_obj = ranked[0]

    if sink and debug_label:
        n_chosen = len(
            _normalize_script_payload(dict(best_obj)).get("parsed_lines") or []
        )
        if best_cc < 500 or n_chosen < 3:
            _debug_dump_llm_parse(
                sink,
                label=debug_label,
                raw=text,
                ranked=ranked,
                chosen=_normalize_script_payload(dict(best_obj)),
            )

    return best_obj


def _finalize_llm_output(
    content: str,
    reasoning: str,
    *,
    sink: PipelineLog,
) -> str:
    """从 content / reasoning / 合并文本中提取剧本 JSON。"""
    orig_c = (content or "").strip()
    orig_r = (reasoning or "").strip()
    content = _repair_utf8_mojibake(orig_c)
    reasoning = _repair_utf8_mojibake(orig_r)
    if content != orig_c or reasoning != orig_r:
        sink.detail("状态: 已修复 SSE 流式响应中的 UTF-8 乱码")
    merged = "\n".join(p for p in (content, reasoning) if p)

    for label, text in (
        ("content", content),
        ("reasoning", reasoning),
        ("合并", merged),
    ):
        if not text:
            continue
        try:
            obj = _extract_best_json_dict(
                text, sink=sink, debug_label=f"finalize_{label}"
            )
            if _score_payload(obj) > 0:
                if label == "content":
                    sink.detail("状态: content 字段含有效剧本 JSON")
                elif label == "reasoning":
                    sink.detail(
                        f"JSON 在 reasoning 中（{len(reasoning)} 字），已提取"
                    )
                else:
                    sink.detail("JSON 在 content+reasoning 合并文本中，已提取")
                if label == "content" and text == content:
                    return content
                return json.dumps(obj, ensure_ascii=False)
        except ValueError:
            continue

    if reasoning and not content:
        raise ThinkingOverflowError(
            f"流结束仍无正文：thinking/reasoning {len(reasoning)} 字，content/text 为空且无 JSON。"
            " 多为 max_tokens 被思考占满：可增大 B2A_CHAPTER_MAX_TOKENS，"
            "或设置 B2A_OPEN_FALLBACK=1 使用 flash JSON Mode。"
        )

    if content:
        return content

    raise ValueError("模型未返回 content 或 reasoning 正文")


def _preview_text(text: str, limit: int = 500) -> str:
    """Safe UTF-8 preview for logs."""
    if not text:
        return ""
    return text[:limit].replace("\r\n", "\n")


def parse_llm_json(
    raw: str,
    *,
    log: PipelineLog | None = None,
    debug_label: str = "",
) -> dict[str, Any]:
    raw = _repair_utf8_mojibake(raw or "")
    label = debug_label or "parse"
    last_error: ValueError | None = None
    for text in (raw, _apply_assistant_prefill(raw)):
        payload, method = parse_script_output(text)
        if payload is not None:
            payload = _normalize_script_payload(payload)
            if log and debug_label:
                n = len(payload.get("parsed_lines") or [])
                cc = sum(
                    _line_content_len(x) for x in (payload.get("parsed_lines") or [])
                )
                log.detail(
                    f"parse_llm_json [{debug_label}]：方式={method}，"
                    f"parsed_lines {n} 行，行内合计 {cc} 字"
                )
            return payload
        try:
            obj = _extract_best_json_dict(text, sink=log, debug_label=label)
            return _normalize_script_payload(obj)
        except ValueError as exc:
            last_error = exc
            continue
    raise ValueError(last_error or "无法解析模型输出（块格式或 JSON）")


def _use_messages_api(model: str) -> bool:
    if STEP_API_MODE == "chat":
        return False
    if STEP_API_MODE == "messages":
        return model.startswith("step-router")
    return model.startswith("step-router")


def step_plan_pipeline_endpoint(model: str | None = None) -> str:
    use_model = model or effective_pipeline_model()
    if use_model.startswith("step-router") and _use_messages_api(use_model):
        return STEP_MESSAGES_URL
    if use_model.startswith("step-router"):
        return STEP_CHAT_URL
    return STEP_OPEN_CHAT_URL


def _split_openai_messages(
    messages: list[dict[str, str]],
) -> tuple[str, list[dict[str, str]]]:
    """OpenAI 风格 messages → Anthropic system + user/assistant 列表。"""
    system_parts: list[str] = []
    dialog: list[dict[str, str]] = []
    for msg in messages:
        role = (msg.get("role") or "user").strip()
        content = str(msg.get("content") or "")
        if role == "system":
            if content:
                system_parts.append(content)
        else:
            dialog.append({"role": role, "content": content})
    if not dialog:
        raise ValueError("Messages API 需要至少一条非 system 消息")
    return "\n\n".join(system_parts), dialog


def _extract_anthropic_content_blocks(
    blocks: list[Any],
) -> tuple[str, str]:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(str(block.get("text") or ""))
        elif btype == "thinking":
            thinking_parts.append(
                str(block.get("thinking") or block.get("text") or "")
            )
    return "".join(text_parts), "".join(thinking_parts)


def _validate_script_raw(raw: str, sink: PipelineLog) -> str:
    repaired = _repair_utf8_mojibake(raw or "")
    try:
        obj = _extract_best_json_dict(repaired, sink=sink, debug_label="validate")
    except ValueError as exc:
        if DEBUG_LLM_RESPONSE:
            _debug_dump_llm_parse(
                sink,
                label="validate_failed",
                raw=repaired,
                ranked=[],
                chosen={"error": str(exc)},
            )
        raise ValueError(
            f"{exc}；预览: {_preview_text(repaired, 300)}"
        ) from exc
    before_lc, before_cc = _payload_script_metrics(obj)
    payload = _normalize_script_payload(obj)
    n_lines = len(payload.get("parsed_lines") or [])
    n_line_chars = sum(
        _line_content_len(x) for x in (payload.get("parsed_lines") or [])
    )
    n_chars = len(payload.get("characters_delta") or [])
    if n_lines > before_lc or n_line_chars > before_cc:
        sink.detail(
            f"已自动修复 JSON 结构：parsed_lines {before_lc}→{n_lines} 行，"
            f"行内 {before_cc}→{n_line_chars} 字（自嵌套/别名字段收拢）"
        )
    if n_lines == 0 and n_chars == 0:
        raise ValueError(
            "JSON 已解析但 parsed_lines 与 characters_delta 均为空；"
            f"顶层 keys={list(payload.keys())}，预览: {_preview_text(repaired, 400)}"
        )
    if n_lines <= 1 and n_line_chars < 500:
        ranked_sparse: list[tuple[int, int, int, dict[str, Any]]] = []
        for obj in _iter_json_dicts_in_text(repaired):
            lc, cc = _payload_script_metrics(obj)
            ranked_sparse.append((_score_payload(obj), lc, cc, obj))
        ranked_sparse.sort(key=lambda t: (t[0], t[2], t[1]), reverse=True)
        _debug_dump_llm_parse(
            sink,
            label="validate_sparse",
            raw=repaired,
            ranked=ranked_sparse,
            chosen=payload,
        )
    if n_lines == 0:
        sink.warn("本块未解析到 parsed_lines，仅更新了角色表")
    return raw


def _build_messages_body(
    model: str,
    messages: list[dict[str, str]],
    profile: str,
) -> dict[str, Any]:
    system_text, dialog = _split_openai_messages(messages)
    body: dict[str, Any] = {
        "model": model,
        "messages": dialog,
    }
    _apply_max_tokens(body, resolve_max_tokens())
    if system_text:
        body["system"] = system_text
    if profile == "router_direct":
        body["temperature"] = 0.15
    elif profile == "router_low_temp":
        body["temperature"] = 0.05
    else:
        body["temperature"] = LLM_TEMPERATURE
    return body


def _messages_streaming(
    api_key: str,
    body: dict[str, Any],
    headers: dict[str, str],
    sink: PipelineLog,
) -> str:
    body = {**body, "stream": True}
    sink.detail("模式: Messages 流式 stream=true（Anthropic SSE）")
    t0 = time.time()
    response = requests.post(
        STEP_MESSAGES_URL,
        headers=headers,
        json=body,
        stream=True,
        timeout=(LLM_CONNECT_TIMEOUT_SEC, LLM_READ_TIMEOUT_SEC),
    )
    header_elapsed = time.time() - t0
    sink.detail(
        f"状态: HTTP 头已返回 ({header_elapsed:.1f}s), status={response.status_code}"
    )
    if response.status_code != 200:
        preview = (response.text or "")[:800]
        raise RuntimeError(f"StepPlan Messages HTTP {response.status_code}: {preview}")

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    stop_reason: str | None = None
    last_beat = time.time()
    saw_thinking = False

    for raw_line in _iter_sse_lines(response):
        now = time.time()
        if now - last_beat >= STREAM_HEARTBEAT_SEC:
            sink.detail(
                f"状态: Messages 流式接收中… {now - t0:.0f}s | "
                f"text {sum(len(p) for p in text_parts)} 字 | "
                f"thinking {sum(len(p) for p in thinking_parts)} 字"
            )
            last_beat = now
        if not raw_line:
            continue
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue

        evt = data.get("type")
        if evt == "content_block_delta":
            delta = data.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                piece = delta.get("text")
                if piece:
                    piece = _repair_utf8_mojibake(piece)
                    if not text_parts:
                        sink.detail(f"状态: 收到首个 text_delta ({now - t0:.1f}s)")
                    text_parts.append(piece)
            elif dtype in ("thinking_delta", "reasoning_delta"):
                piece = (
                    delta.get("thinking")
                    or delta.get("text")
                    or delta.get("reasoning")
                    or ""
                )
                if piece:
                    if not saw_thinking:
                        sink.detail("状态: 检测到 thinking 块（与 text 分离）")
                        saw_thinking = True
                    thinking_parts.append(piece)
                    thinking_so_far = "".join(thinking_parts)
                    if _thinking_should_abort(
                        thinking_so_far, sum(len(p) for p in text_parts)
                    ):
                        response.close()
                        raise ThinkingOverflowError(
                            f"thinking 已达 {len(thinking_so_far)} 字仍无 text/JSON，提前中止"
                        )
        elif evt == "message_delta":
            delta = data.get("delta") or {}
            sr = delta.get("stop_reason") or data.get("stop_reason")
            if sr:
                stop_reason = sr

    content = "".join(text_parts)
    thinking = "".join(thinking_parts)
    if not content.strip() and not thinking.strip():
        raise RuntimeError("Messages 流式响应无 text/thinking 正文")

    sink.detail(
        f"状态: Messages 流式完成 text={len(content)} 字, thinking={len(thinking)} 字, "
        f"耗时 {time.time() - t0:.1f}s"
        + (f", stop_reason={stop_reason}" if stop_reason else "")
    )
    if stop_reason == "max_tokens":
        sink.warn("stop_reason=max_tokens：输出可能被截断，可增大 B2A_MAX_TOKENS")

    raw = _finalize_llm_output(content, thinking, sink=sink)
    return _validate_script_raw(raw, sink)


def _messages_blocking(
    api_key: str,
    body: dict[str, Any],
    headers: dict[str, str],
    sink: PipelineLog,
) -> str:
    body = {**body, "stream": False}
    sink.detail("模式: Messages 非流式 stream=false")
    t0 = time.time()
    response = requests.post(
        STEP_MESSAGES_URL,
        headers=headers,
        json=body,
        timeout=(LLM_CONNECT_TIMEOUT_SEC, LLM_READ_TIMEOUT_SEC),
    )
    elapsed = time.time() - t0
    sink.detail(f"状态: 已收到完整响应 ({elapsed:.1f}s), HTTP {response.status_code}")
    if response.status_code != 200:
        raise RuntimeError(
            f"StepPlan Messages HTTP {response.status_code}: "
            f"{(response.text or '')[:1200]}"
        )
    response.encoding = "utf-8"
    data = response.json()
    content, thinking = _extract_anthropic_content_blocks(data.get("content") or [])
    stop_reason = data.get("stop_reason")
    usage = data.get("usage", {})
    sink.detail(
        f"状态: 回收成功 text={len(content)} thinking={len(thinking)}"
        + (f", stop_reason={stop_reason}" if stop_reason else "")
        + (f", usage={usage}" if usage else "")
    )
    if stop_reason == "max_tokens":
        sink.warn("stop_reason=max_tokens：输出可能被截断，可增大 B2A_MAX_TOKENS")
    raw = _finalize_llm_output(content, thinking, sink=sink)
    return _validate_script_raw(raw, sink)


def _dispatch_messages(
    api_key: str,
    body: dict[str, Any],
    headers: dict[str, str],
    sink: PipelineLog,
) -> str:
    try:
        return _messages_streaming(api_key, body, headers, sink)
    except (requests.RequestException, json.JSONDecodeError) as exc:
        sink.warn(f"Messages 流式失败，尝试非流式: {exc}")
        return _messages_blocking(api_key, body, headers, sink)


def _step_messages_completion(
    api_key: str,
    messages: list[dict[str, str]],
    *,
    log: PipelineLog | None = None,
    model: str | None = None,
) -> str:
    sink = log or PipelineLog()
    use_model = model or LLM_MODEL
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }
    user_chars = sum(len(m.get("content", "")) for m in messages)
    est_tokens = max(1, user_chars // 2)

    sink.detail(f"API 端点: POST {STEP_MESSAGES_URL}")
    sink.detail(f"API 协议: Anthropic Messages（Step Plan）")
    sink.detail(f"模型: {use_model}")
    sink.detail(
        "step-router-v1：复杂任务路由 deepseek-v4-pro；"
        "正文应出现在 content[].type=text 块"
    )
    sink.detail(
        f"请求: system+user, 约 {user_chars} 字 "
        f"(粗估 ~{est_tokens} tokens), max_tokens={_format_max_tokens_label(resolve_max_tokens())}"
    )
    sink.detail(f"切片配置: CHUNK_SIZE={CHUNK_SIZE}, OVERLAP={CHUNK_OVERLAP}")

    last_error: Exception | None = None
    body = _build_messages_body(use_model, messages, "router_direct")
    sink.detail(
        f"请求配置: Messages · step-router-v1 · "
        f"max_tokens={_format_max_tokens_label(resolve_max_tokens())} "
        f"(文档上限 {ROUTER_MAX_TOKENS_CAP}) · 不提前中止 thinking"
    )

    for attempt in range(1, LLM_MAX_RETRIES + 1):
        sink.detail(f"── HTTP 请求 {attempt}/{LLM_MAX_RETRIES} [messages] ──")
        sink.detail("状态: 正在发送 POST…")
        stop_heartbeat = threading.Event()

        def _heartbeat() -> None:
            t0 = time.time()
            while not stop_heartbeat.wait(STREAM_HEARTBEAT_SEC):
                sink.detail(f"状态: 仍在等待 API… 已等待 {time.time() - t0:.0f}s")

        hb = threading.Thread(target=_heartbeat, daemon=True)
        hb.start()
        try:
            return _dispatch_messages(api_key, body, headers, sink)
        except ThinkingOverflowError as exc:
            last_error = exc
            sink.warn(str(exc))
            break
        except ValueError as exc:
            last_error = exc
            sink.warn(f"输出无法解析为有效剧本 JSON: {exc}")
            break
        except requests.exceptions.ConnectTimeout as exc:
            sink.error(f"连接超时: {exc}")
            last_error = exc
            time.sleep(6 * attempt)
        except requests.exceptions.ReadTimeout as exc:
            sink.error(f"读取超时: {exc}")
            last_error = exc
            time.sleep(6 * attempt)
        except requests.exceptions.RequestException as exc:
            sink.error(f"网络异常: {exc}")
            last_error = exc
            time.sleep(6 * attempt)
        except RuntimeError as exc:
            sink.error(str(exc))
            last_error = exc
            if "HTTP 400" in str(exc):
                break
            if "HTTP 4" in str(exc) and "429" not in str(exc):
                raise
            time.sleep(8 * attempt)
        finally:
            stop_heartbeat.set()
    sink.warn("Messages 未产出 JSON，回退 Step Plan Chat Completions…")
    return step_chat_completion(
        api_key, messages, log=sink, model=use_model, _skip_messages=True
    )


def _response_model_from_chat_obj(obj: dict[str, Any]) -> str | None:
    """从 Chat Completion 响应/SSE 块读取 model（router 时可能是实际后端名）。"""
    name = (obj.get("model") or "").strip()
    return name or None


def _log_routed_model_if_any(
    sink: PipelineLog,
    *,
    request_model: str,
    response_model: str | None,
) -> None:
    if not response_model:
        return
    if response_model == request_model:
        sink.detail(f"响应 model={response_model}（与请求一致）")
        return
    sink.progress(
        f"Router 实际后端：{response_model}（请求入口 {request_model}）"
    )


def _parse_sse_payload(
    payload: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Parse SSE JSON payload.
    Returns (content_piece, reasoning_piece, debug_hint, response_model).
    step-router 可能长时间只推送 reasoning_content，content 为空。
    """
    if not payload or payload == "[DONE]":
        return None, None, None, None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None, None, f"json_error:{payload[:80]}", None

    response_model = _response_model_from_chat_obj(obj)
    choices = obj.get("choices") or []
    if not choices:
        return None, None, "no_choices", response_model

    choice = choices[0]
    delta = choice.get("delta") or {}
    message = choice.get("message") or {}

    content = delta.get("content") or message.get("content")
    reasoning = (
        delta.get("reasoning_content")
        or delta.get("reasoning")
        or message.get("reasoning_content")
        or message.get("reasoning")
    )

    if content or reasoning:
        return content, reasoning, None, response_model

    # 空 delta（常见心跳包）
    if not delta and not message:
        return None, None, "empty_delta", response_model
    keys = ",".join(delta.keys()) if isinstance(delta, dict) else "delta"
    return None, None, f"delta_keys={keys}", response_model


def _raise_stepplan_http_error(status_code: int, preview: str) -> None:
    """451 / censorship_blocked → CensorshipBlockedError，供敏感内容二分逻辑捕获。"""
    from utils.sensitive_content import CensorshipBlockedError

    text = preview or ""
    if status_code == 451 or "censorship_blocked" in text.lower():
        sink_preview = text[:500]
        raise CensorshipBlockedError(
            f"StepPlan HTTP {status_code}: {sink_preview}",
            status_code=status_code,
        )
    raise RuntimeError(f"StepPlan HTTP {status_code}: {text[:1200]}")


def _chat_completion_streaming(
    api_key: str,
    body: dict[str, Any],
    headers: dict[str, str],
    sink: PipelineLog,
    *,
    read_timeout: int = LLM_READ_TIMEOUT_SEC,
    thinking_abort_chars: int | None = None,
) -> str:
    """Stream response so headers arrive early and we can log incremental progress."""
    body = {**body, "stream": True}
    sink.detail("模式: 流式 stream=true（可尽早确认服务端已开始生成）")
    t0 = time.time()
    response = requests.post(
        STEP_CHAT_URL,
        headers=headers,
        json=body,
        stream=True,
        timeout=(LLM_CONNECT_TIMEOUT_SEC, read_timeout),
    )
    header_elapsed = time.time() - t0
    sink.detail(
        f"状态: HTTP 头已返回 ({header_elapsed:.1f}s), status={response.status_code}"
    )

    if response.status_code != 200:
        preview = (response.text or "")[:500]
        sink.detail(
            f"StepPlan HTTP {response.status_code}: {preview}"
        )
        _raise_stepplan_http_error(response.status_code, preview)

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    last_beat = time.time()
    last_data_at = t0
    raw_debug: list[str] = []
    saw_reasoning = False
    finish_reason: str | None = None
    response_model_seen: str | None = None
    request_model = str(body.get("model") or LLM_MODEL)

    for raw_line in _iter_sse_lines(response):
        now = time.time()
        if raw_line.strip():
            last_data_at = now
        if now - last_data_at >= read_timeout:
            response.close()
            raise RuntimeError(
                f"API 流式连续 {read_timeout}s 无新数据（无响应超时），"
                "请检查网络或稍后断点续跑。"
            )
        if now - last_beat >= STREAM_HEARTBEAT_SEC:
            c_len = sum(len(p) for p in content_parts)
            r_len = sum(len(p) for p in reasoning_parts)
            sink.progress(
                f"流式接收中 {now - t0:.0f}s · content {c_len} 字 · "
                f"reasoning {r_len} 字"
            )
            last_beat = now

        if not raw_line:
            continue
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload and payload != "[DONE]":
            try:
                fr = (json.loads(payload).get("choices") or [{}])[0].get(
                    "finish_reason"
                )
                if fr:
                    finish_reason = fr
            except json.JSONDecodeError:
                pass
        content_piece, reasoning_piece, hint, chunk_model = _parse_sse_payload(payload)
        if chunk_model:
            response_model_seen = chunk_model

        if hint and len(raw_debug) < 8:
            raw_debug.append(hint)

        if reasoning_piece:
            reasoning_piece = _repair_utf8_mojibake(reasoning_piece)
            if not saw_reasoning:
                sink.progress(
                    "模型思考中（reasoning），JSON 正文稍后才开始…"
                )
                saw_reasoning = True
            reasoning_parts.append(reasoning_piece)
            reasoning_so_far = "".join(reasoning_parts)
            if _thinking_should_abort(
                reasoning_so_far,
                sum(len(p) for p in content_parts),
                limit=thinking_abort_chars,
                elapsed_sec=now - t0,
            ):
                response.close()
                raise ThinkingOverflowError(
                    f"reasoning 已达 {len(reasoning_so_far)} 字仍无 content/JSON，提前中止"
                )

        if content_piece:
            content_piece = _repair_utf8_mojibake(content_piece)
            if not content_parts:
                sink.progress(f"开始接收 JSON 正文（{now - t0:.0f}s）")
            content_parts.append(content_piece)

    content = "".join(content_parts)
    reasoning = "".join(reasoning_parts)

    if not content.strip() and not reasoning.strip():
        dbg = "; ".join(raw_debug) if raw_debug else "无 SSE 解析线索"
        raise RuntimeError(f"流式响应无正文。SSE 线索: {dbg}")

    sink.detail(
        f"状态: 流式完成 content={len(content)} 字, reasoning={len(reasoning)} 字, "
        f"耗时 {time.time() - t0:.1f}s"
        + (f", finish_reason={finish_reason}" if finish_reason else "")
    )
    _log_routed_model_if_any(
        sink,
        request_model=request_model,
        response_model=response_model_seen,
    )
    if not response_model_seen:
        sink.detail(
            "未从 SSE 块中解析到响应 model 字段；"
            "可在非流式响应或 Step 控制台用量里确认实际后端"
        )
    if DEBUG_LLM_RESPONSE and (content.strip() or reasoning.strip()):
        debug_dir = LOG_DIR / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        if content.strip():
            p = debug_dir / f"{stamp}_stream_content.txt"
            p.write_text(content, encoding="utf-8")
            sink.detail(f"已保存 API content 原文：{p.name}（{len(content)} 字）")
        if reasoning.strip():
            p = debug_dir / f"{stamp}_stream_reasoning.txt"
            text = reasoning if len(reasoning) <= 500_000 else reasoning[:500_000] + "\n…(truncated)"
            p.write_text(text, encoding="utf-8")
            sink.detail(f"已保存 API reasoning：{p.name}（{len(reasoning)} 字）")
    if finish_reason == "length":
        sink.warn("finish_reason=length：输出可能被截断，可增大 B2A_MAX_TOKENS")
    if finish_reason == "content_filter":
        sink.warn(
            "finish_reason=content_filter：输出被安全策略截断，"
            "将按子段覆盖率继续切片或记入待手动录入"
        )
    raw = _finalize_llm_output(content, reasoning, sink=sink)
    return _validate_script_raw(raw, sink)


def _chat_completion_blocking(
    api_key: str,
    body: dict[str, Any],
    headers: dict[str, str],
    sink: PipelineLog,
    *,
    read_timeout: int = LLM_READ_TIMEOUT_SEC,
) -> str:
    """Non-streaming fallback."""
    body = {**body, "stream": False}
    sink.detail("模式: 非流式 stream=false")
    t0 = time.time()
    response = requests.post(
        STEP_CHAT_URL,
        headers=headers,
        json=body,
        timeout=(LLM_CONNECT_TIMEOUT_SEC, read_timeout),
    )
    elapsed = time.time() - t0
    sink.detail(f"状态: 已收到完整响应 ({elapsed:.1f}s), HTTP {response.status_code}")
    if response.status_code != 200:
        preview = (response.text or "")[:1200]
        _raise_stepplan_http_error(response.status_code, preview)
    response.encoding = "utf-8"
    data = response.json()
    _log_routed_model_if_any(
        sink,
        request_model=str(body.get("model") or LLM_MODEL),
        response_model=_response_model_from_chat_obj(data),
    )
    choice0 = data["choices"][0]
    fr = choice0.get("finish_reason")
    if fr:
        sink.detail(f"finish_reason={fr}")
        if fr == "length":
            sink.warn("finish_reason=length：输出可能被截断，可增大 B2A_MAX_TOKENS")
    msg = choice0["message"]
    content = str(msg.get("content") or "")
    reasoning = str(
        msg.get("reasoning_content") or msg.get("reasoning") or ""
    )
    usage = data.get("usage", {})
    sink.detail(
        f"状态: 回收成功 content={len(content)} reasoning={len(reasoning)}"
        + (f", usage={usage}" if usage else "")
    )
    raw = _finalize_llm_output(content, reasoning, sink=sink)
    return _validate_script_raw(raw, sink)


def _request_profiles(model: str) -> list[str]:
    """Step Plan 上 step-router-v1 不接受 json_object；用不同 profile 依次尝试。"""
    if model.startswith("step-router"):
        return ["router_direct", "router_low_temp"]
    return ["json_object"]


def _format_max_tokens_label(tokens: int | None) -> str:
    if tokens is None:
        return "模型默认（未传 max_tokens）"
    return str(tokens)


def _apply_max_tokens(body: dict[str, Any], tokens: int | None) -> None:
    if tokens is not None:
        body["max_tokens"] = tokens
    else:
        body.pop("max_tokens", None)


def _build_chat_body(
    model: str,
    messages: list[dict[str, str]],
    profile: str,
) -> dict[str, Any]:
    """
    step-router-v1（Step Plan 专用）：
    - 不支持 response_format=json_object（HTTP 400）
    - deepseek-style 会把全文打进 reasoning，易占满 max_tokens 且无 JSON
    - 使用默认 general：JSON 由 Prompt 约束，尽量落在 content
    """
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if profile == "json_object":
        body["temperature"] = LLM_TEMPERATURE
        body["response_format"] = {"type": "json_object"}
        _apply_max_tokens(body, LLM_MAX_TOKENS)
        return body
    if profile in ("router_direct", "router_low_temp"):
        body["temperature"] = 0.1
        # 显式 general，避免默认走 deepseek-style 把输出打进 reasoning_content
        body["reasoning_format"] = "general"
        return body
    raise ValueError(f"未知请求 profile: {profile}")


def _describe_profile(model: str, profile: str) -> str:
    if profile == "json_object":
        return f"{model} + response_format=json_object"
    if profile == "router_direct":
        return (
            f"{model}（Prompt 约束 JSON，无 json_object / 无 deepseek-style 思考链）"
        )
    if profile == "router_low_temp":
        return f"{model}（低温度 {0.05} 直出 JSON）"
    return profile


def _dispatch_chat_completion(
    api_key: str,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    sink: PipelineLog,
    *,
    read_timeout: int = LLM_READ_TIMEOUT_SEC,
    thinking_abort_chars: int | None = None,
) -> str:
    try:
        return _chat_completion_streaming(
            api_key,
            body,
            headers,
            sink,
            read_timeout=read_timeout,
            thinking_abort_chars=thinking_abort_chars,
        )
    except (requests.RequestException, json.JSONDecodeError) as exc:
        sink.warn(f"流式请求失败，尝试非流式: {exc}")
        return _chat_completion_blocking(
            api_key, body, headers, sink, read_timeout=read_timeout
        )


def _step_open_json_completion(
    api_key: str,
    messages: list[dict[str, str]],
    *,
    log: PipelineLog | None = None,
    model: str | None = None,
) -> str:
    """开放平台 Chat Completions + JSON Mode（剧本拆解推荐路径）。"""
    sink = log or PipelineLog()
    use_model = model or OPEN_FALLBACK_MODEL
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }
    body = {
        "model": use_model,
        "messages": messages,
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }
    user_chars = sum(len(m.get("content", "")) for m in messages)
    sink.detail(f"API 端点: POST {STEP_OPEN_CHAT_URL}")
    sink.detail(f"模型: {use_model} · 开放平台 JSON Mode（仅 B2A_OPEN_FALLBACK=1 时启用）")
    sink.detail(
        f"请求: {len(messages)} 条消息, 约 {user_chars} 字, max_tokens={LLM_MAX_TOKENS}"
    )
    return _dispatch_chat_completion(
        api_key, STEP_OPEN_CHAT_URL, body, headers, sink
    )


def step_chat_completion(
    api_key: str,
    messages: list[dict[str, str]],
    *,
    log: PipelineLog | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
    read_timeout: int | None = None,
    thinking_abort_chars: int | None = None,
    _skip_messages: bool = False,
) -> str:
    """Step Plan 剧本拆解：默认 step-router-v1 @ step_plan/v1。"""
    sink = log or PipelineLog()
    use_model = model or LLM_MODEL

    if not use_model.startswith("step-router"):
        if ENABLE_OPEN_FALLBACK:
            return _step_open_json_completion(api_key, messages, log=sink, model=use_model)
        raise ValueError(f"非 router 模型 {use_model} 需在 Step Plan 使用 step-router-v1")

    if not _skip_messages and _use_messages_api(use_model):
        return _step_messages_completion(
            api_key, messages, log=sink, model=use_model
        )

    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }
    user_chars = sum(len(m.get("content", "")) for m in messages)
    est_tokens = max(1, user_chars // 2)

    sink.detail(f"API 端点: POST {STEP_CHAT_URL}")
    sink.detail(f"API 基址: {STEP_API_BASE}（Step Plan 专用，仅 model=step-router-v1）")
    sink.detail(f"模型: {use_model}")
    sink.detail(
        "step-router-v1 自动路由 deepseek-v4-pro / step-3.5-flash；"
        "不设 reasoning_format=deepseek-style；等待 thinking 结束后的 content"
    )
    sink.detail(
        f"max_tokens={_format_max_tokens_label(resolve_max_tokens())} "
        f"（文档上限 {ROUTER_MAX_TOKENS_CAP}）"
    )
    sink.detail(
        f"请求: {len(messages)} 条消息, 约 {user_chars} 字 "
        f"(粗估 ~{est_tokens} tokens)"
    )
    sink.detail(f"切片: CHUNK_SIZE={CHUNK_SIZE}, OVERLAP={CHUNK_OVERLAP}")
    if THINKING_ABORT_CHARS > 0:
        sink.detail(f"thinking 提前中止阈值: {THINKING_ABORT_CHARS} 字")
    else:
        sink.detail("thinking 提前中止: 关闭（允许思考完成后再收 content）")
    sink.detail(
        f"超时: 连接 {LLM_CONNECT_TIMEOUT_SEC}s / 读取 {LLM_READ_TIMEOUT_SEC}s"
    )

    from utils.sensitive_content import CensorshipBlockedError

    last_error: Exception | None = None
    use_max_tokens = resolve_max_tokens(max_tokens)
    use_read_timeout = read_timeout if read_timeout is not None else LLM_READ_TIMEOUT_SEC
    body = _build_chat_body(use_model, messages, "router_direct")
    _apply_max_tokens(body, use_max_tokens)

    for attempt in range(1, LLM_MAX_RETRIES + 1):
        sink.detail(f"── HTTP 请求 {attempt}/{LLM_MAX_RETRIES} [router_chat] ──")
        sink.progress(
            f"已发送 API 请求（max_tokens={_format_max_tokens_label(use_max_tokens)}，"
            f"读取超时 {use_read_timeout}s）…"
        )

        stop_heartbeat = threading.Event()
        wait_t0 = time.time()

        def _heartbeat() -> None:
            while not stop_heartbeat.wait(STREAM_HEARTBEAT_SEC):
                sink.progress(
                    f"仍在等待模型响应… 已 {time.time() - wait_t0:.0f}s"
                )

        hb = threading.Thread(target=_heartbeat, daemon=True)
        hb.start()
        try:
            return _dispatch_chat_completion(
                api_key,
                STEP_CHAT_URL,
                body,
                headers,
                sink,
                read_timeout=use_read_timeout,
                thinking_abort_chars=thinking_abort_chars,
            )
        except ThinkingOverflowError as exc:
            last_error = exc
            sink.warn(str(exc))
            raise
        except CensorshipBlockedError:
            raise
        except ValueError as exc:
            last_error = exc
            sink.warn(f"输出无法解析为剧本 JSON: {exc}")
            break
        except requests.exceptions.ConnectTimeout as exc:
            sink.error(f"连接超时: {exc}")
            last_error = exc
            time.sleep(6 * attempt)
        except requests.exceptions.ReadTimeout as exc:
            sink.error(f"读取超时（>{use_read_timeout}s）: {exc}")
            last_error = exc
            time.sleep(6 * attempt)
        except requests.exceptions.RequestException as exc:
            sink.error(f"网络异常: {exc}")
            last_error = exc
            time.sleep(6 * attempt)
        except RuntimeError as exc:
            last_error = exc
            msg = str(exc)
            if "HTTP 500" in msg:
                sink.warn(f"StepPlan 服务端 500，{10 * attempt}s 后重试…")
                time.sleep(10 * attempt)
                continue
            sink.error(msg)
            if "HTTP 400" in msg:
                break
            if "HTTP 4" in msg and "429" not in msg:
                raise
            time.sleep(8 * attempt)
        finally:
            stop_heartbeat.set()

    if ENABLE_OPEN_FALLBACK:
        return _step_open_json_completion(api_key, messages, log=sink)

    raise RuntimeError(f"StepPlan router 拆解失败: {last_error}")


def step_chapter_completion(
    api_key: str,
    messages: list[dict[str, str]],
    *,
    log: PipelineLog | None = None,
) -> str:
    """按章拆解：默认不传 max_tokens，由模型决定输出上限。"""
    sink = log or PipelineLog()
    msgs = [dict(m) for m in messages]
    tokens = resolve_chapter_max_tokens()
    sink.progress(
        f"本章 API（max_tokens={_format_max_tokens_label(tokens)}，"
        f"文档上限约 {ROUTER_MAX_TOKENS_CAP}）…"
    )
    if CHAPTER_THINKING_ABORT_CHARS > 0:
        sink.detail(
            f"本章 thinking 提前中止: {CHAPTER_THINKING_ABORT_CHARS} 字"
            f"（或 reasoning ≥{CHAPTER_THINKING_ABORT_MIN_CHARS} 字且"
            f" {CHAPTER_THINKING_ABORT_TIME_SEC}s 仍无 content）"
        )
    else:
        sink.detail("本章 thinking 提前中止: 关闭")

    try:
        return step_chat_completion(
            api_key,
            msgs,
            log=sink,
            max_tokens=tokens,
            read_timeout=CHAPTER_READ_TIMEOUT_SEC,
            thinking_abort_chars=CHAPTER_THINKING_ABORT_CHARS or None,
        )
    except ThinkingOverflowError as exc:
        last_error: Exception = exc
        sink.warn(f"首次调用失败: {exc}")
        if msgs and msgs[-1].get("role") == "user":
            msgs[-1] = {
                **msgs[-1],
                "content": (msgs[-1].get("content") or "") + CHAPTER_DIRECT_JSON_SUFFIX,
            }
        sink.progress(
            f"重试本章 API（max_tokens={_format_max_tokens_label(tokens)}）…"
        )
        try:
            return step_chat_completion(
                api_key,
                msgs,
                log=sink,
                max_tokens=tokens,
                read_timeout=CHAPTER_READ_TIMEOUT_SEC,
                thinking_abort_chars=CHAPTER_THINKING_ABORT_CHARS or None,
            )
        except ThinkingOverflowError as exc2:
            last_error = exc2
            sink.warn(f"重试仍失败: {exc2}")

    if ENABLE_OPEN_FALLBACK:
        sink.progress("router 无 JSON，改用开放平台 step-3.5-flash（JSON Mode）…")
        return _step_open_json_completion(api_key, messages, log=sink)

    raise RuntimeError(f"本章拆解失败: {last_error}")


def llm_parse_chapter_payload(
    api_key: str,
    chapter: NovelChapter,
    total_chapters: int,
    memory_json: str,
    *,
    incomplete_names: list[str] | None = None,
    force_split: bool = False,
    log: PipelineLog | None = None,
) -> dict[str, Any]:
    """对一章（或超长章的多段）调用 LLM 并合并 JSON。"""
    sink = log or PipelineLog()
    if force_split:
        segments = slice_chapter_for_api(chapter, force=True)
    elif len(chapter.text) <= CHAPTER_SINGLE_SHOT_MAX:
        segments = [chapter]
    else:
        segments = slice_chapter_for_api(chapter)
    if len(segments) > 1:
        sink.progress(
            f"第 {chapter.chapter_num} 章约 {len(chapter.text)} 字，"
            f"拆为 {len(segments)} 段请求（每段约 {CHAPTER_SUB_SLICE_SIZE} 字）"
        )

    merged_lines: list[Any] = []
    merged_delta: list[Any] = []

    for seg in segments:
        if len(segments) > 1:
            sink.progress(
                f"第 {chapter.chapter_num} 章 · 第 {seg.index}/{len(segments)} 段"
                f"（{len(seg.text)} 字）…"
            )
        prompt = build_chapter_user_prompt(
            seg,
            total_chapters,
            memory_json,
            incomplete_names=incomplete_names,
        )
        raw = step_chapter_completion(
            api_key,
            [
                {"role": "system", "content": SOP_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            log=sink,
        )
        part = expand_parsed_lines_in_payload(
            parse_llm_json(
                raw,
                log=sink,
                debug_label=f"ch{chapter.chapter_num}_seg{seg.index}",
            )
        )
        merged_lines.extend(part.get("parsed_lines") or [])
        merged_delta.extend(part.get("characters_delta") or [])

    return {"parsed_lines": merged_lines, "characters_delta": merged_delta}


def refine_chapter_payload_after_parse(
    api_key: str,
    chapter: NovelChapter,
    total: int,
    memory_json: str,
    payload: dict[str, Any],
    *,
    incomplete_names: list[str] | None = None,
    log: PipelineLog | None = None,
) -> tuple[dict[str, Any], list[Any], list[Any]]:
    """覆盖率不足时：补全重试；仍不足则对缺口做敏感切片。失败时保留已有行并继续。"""
    from utils.sensitive_content import (
        is_censorship_blocked_error,
        sensitive_fill_chapter_gaps,
    )

    sink = log or PipelineLog()
    payload = expand_parsed_lines_in_payload(payload)
    parsed = payload.get("parsed_lines") or []
    extra_blocked: list[Any] = []
    coverage = chapter_parse_coverage(chapter, parsed)
    if not chapter_output_too_short(chapter, parsed, coverage):
        return payload, parsed, extra_blocked

    combined = sum(
        len(str(item.get("content") or ""))
        for item in parsed
        if isinstance(item, dict)
    )
    reason = (
        f"覆盖率 {coverage:.1%} 低于 {CHAPTER_COVERAGE_MIN:.0%}"
        if coverage < CHAPTER_COVERAGE_MIN
        else "parsed_lines 行数过少"
    )
    sink.warn(
        f"第 {chapter.chapter_num} 章需补全（{reason}）：{len(parsed)} 行，"
        f"去标点原文 {len(text_without_punctuation(_chapter_target_text(chapter)))} 字，"
        f"剧本行内 {combined} 字"
    )

    try:
        raw_retry = step_chapter_completion(
            api_key,
            [
                {"role": "system", "content": SOP_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_chapter_coverage_retry_prompt(chapter),
                },
            ],
            log=sink,
        )
        payload_retry = expand_parsed_lines_in_payload(
            parse_llm_json(
                raw_retry,
                log=sink,
                debug_label=f"ch{chapter.chapter_num}_retry",
            )
        )
        parsed_retry = payload_retry.get("parsed_lines") or []
        cov_retry = chapter_parse_coverage(chapter, parsed_retry)
        if len(parsed_retry) > len(parsed) or cov_retry > coverage + 0.02:
            sink.progress(
                f"补全重试成功：{len(parsed)} → {len(parsed_retry)} 行，"
                f"覆盖率 {coverage:.1%} → {cov_retry:.1%}"
            )
            if not chapter_output_too_short(chapter, parsed_retry, cov_retry):
                return payload_retry, parsed_retry, extra_blocked
            payload_retry, gap_blocked = sensitive_fill_chapter_gaps(
                api_key,
                chapter,
                total,
                memory_json,
                payload_retry,
                incomplete_names=incomplete_names,
                log=sink,
                coverage_min=CHAPTER_COVERAGE_MIN,
            )
            extra_blocked.extend(gap_blocked)
            return payload_retry, payload_retry.get("parsed_lines") or [], extra_blocked
    except Exception as exc:
        if is_censorship_blocked_error(exc):
            sink.warn(f"补全重试因审核拦截跳过，将尝试缺口切片: {exc}")
        else:
            sink.warn(f"补全重试未成功，保留首次结果: {exc}")

    if (coverage < CHAPTER_COVERAGE_MIN or len(parsed) < 3) and len(
        chapter.text
    ) > 1200:
        try:
            payload_gap, gap_blocked = sensitive_fill_chapter_gaps(
                api_key,
                chapter,
                total,
                memory_json,
                payload,
                incomplete_names=incomplete_names,
                log=sink,
                coverage_min=CHAPTER_COVERAGE_MIN,
            )
            extra_blocked.extend(gap_blocked)
            parsed_gap = payload_gap.get("parsed_lines") or []
            cov_gap = chapter_parse_coverage(chapter, parsed_gap)
            if len(parsed_gap) > len(parsed) or cov_gap > coverage + 0.02:
                sink.progress(
                    f"缺口切片完成：{len(parsed)} → {len(parsed_gap)} 行，"
                    f"覆盖率 {coverage:.1%} → {cov_gap:.1%}"
                )
                return payload_gap, parsed_gap, extra_blocked
        except Exception as exc:
            if is_censorship_blocked_error(exc):
                sink.warn(f"缺口敏感切片未完全成功（已跳过无法生成的子段）: {exc}")
            else:
                sink.warn(f"缺口敏感切片失败: {exc}")

    return payload, parsed, extra_blocked


_CHAIN_MEMORY_USER_NOTE = """
【链式人设演进】下方为已落库演员档案（importance_level：main=当前累计对白量 Top 14，extra=龙套池）。
须结合本章原文更新、合并、深化 personality 与代表台词，禁止原样照抄旧档案。
每人 personality **不得超过 300 字**（含标点）：在旧档案基础上合并本章新信息后，输出**替换后的完整精炼侧写**，禁止在旧文后追加段落导致变长。
【物理隔离重申】对白 content 仅含双引号内原话；引导语/动作/神态一律旁白。
复合句必须三行式原子切分（参考 System 中白大褂/梁愿醒 Few-Shot）。输出前逐行自检四大红线。
"""


def build_memory_block(characters: list[dict[str, Any]]) -> str:
    if not characters:
        return "（暂无，这是全书第一块文本。）"
    slim = []
    for row in characters:
        slim.append(
            {
                "name": row.get("name", ""),
                "gender": row.get("gender", ""),
                "age": row.get("age", ""),
                "personality": row.get("personality", ""),
                "quote_1": row.get("quote_1", ""),
                "quote_2": row.get("quote_2", ""),
                "quote_1_instruction": row.get("quote_1_instruction", ""),
                "quote_2_instruction": row.get("quote_2_instruction", ""),
                "importance_level": row.get("importance_level") or "pending",
            }
        )
    return json.dumps(slim, ensure_ascii=False, indent=2)


def _build_preamble_prompt_block(chapter: NovelChapter) -> str:
    """第一章含书前信息时的额外说明。"""
    n = chapter.preamble_chars
    if n <= 0:
        return ""
    return f"""
【书前信息（须拆解）】
- 本章原文开头约 {n} 字位于「第一章」标题之前（书名、版权、前言等），属于本章 parsed_lines 范围。
- 须从本章第一个字起逐字输出 parsed_lines；书名、前言与章标题后的正文同等对待。
- 禁止只输出书名或章题后停止；不得将书前信息视为可跳过的元数据。
"""


def build_chapter_user_prompt(
    chapter: NovelChapter,
    total_chapters: int,
    memory_json: str,
    *,
    incomplete_names: list[str] | None = None,
) -> str:
    incomplete_block = ""
    if incomplete_names:
        incomplete_block = f"""
【待补全人设的角色】（须在本章 characters_delta 中为下列角色写出完整档案，禁止仅填 name）
{json.dumps(incomplete_names, ensure_ascii=False)}
"""
    if chapter.slice_mode == "fixed" and chapter.overlap_prefix > 0:
        target_text = chapter.text[chapter.overlap_prefix :]
        return f"""【任务上下文】
- 当前为全书第 {chapter.index}/{total_chapters} 段（虚拟第 {chapter.chapter_num} 段，无「第N章」标题，按字数切分）
- 本段在全书中的字符区间：[{chapter.start}, {chapter.end})
- 本段原文字数：{len(chapter.text)} 字（其中前 {chapter.overlap_prefix} 字为与上一段重叠的上下文）

{_CHAIN_MEMORY_USER_NOTE}
【前文已知记忆（链式演进基底）】
{memory_json}
{incomplete_block}
【本段完整文本（含重叠上下文，供理解衔接）】
{chapter.text}

【待拆解正文（仅针对此部分输出 parsed_lines，不得重复上一段已拆解内容）】
{target_text}

【输出要求】
- 表格化抽取，不是文学分析；思考完成后必须输出完整 B2A 块（###B2A### … ###END###）。
- 每条 [line] 的 content<<<…>>> 仅覆盖「待拆解正文」，与原文逐字一致（含标点、引号）。
- 不要 Markdown，不要 JSON。
- 所有行均归属虚拟第 {chapter.chapter_num} 段（chapter_num={chapter.chapter_num}）。"""

    preamble_block = _build_preamble_prompt_block(chapter)
    return f"""【任务上下文】
- 当前为全书第 {chapter.index}/{total_chapters} 次拆解（小说第 {chapter.chapter_num} 章）
- 本章在全书中的字符区间：[{chapter.start}, {chapter.end})
- 本章原文字数：{len(chapter.text)} 字
{preamble_block}
{_CHAIN_MEMORY_USER_NOTE}
【前文已知记忆（链式演进基底）】
{memory_json}
{incomplete_block}
【本章小说原文（完整一章，须全部拆解）】
{chapter.text}

【输出要求】
- 表格化抽取，不是文学分析；思考完成后必须输出完整 B2A 块（###B2A### … ###END###）。
- 每条 [line] 的 content<<<…>>> 须覆盖本章全部正文（含书前信息与章标题至章末），与原文逐字一致。
- 不要 Markdown，不要 JSON。
- 所有行均属于第 {chapter.chapter_num} 章，勿输出其他章节内容或重复前章已拆解正文。"""


_CHARACTER_PROFILE_FIELDS = (
    "gender",
    "age",
    "personality",
    "quote_1",
    "quote_2",
)


def character_has_profile(row: dict[str, Any]) -> bool:
    """至少具备性别与人设侧写，视为演员档案完整。"""
    return bool((row.get("gender") or "").strip()) and bool(
        (row.get("personality") or "").strip()
    )


def delta_has_profile(item: dict[str, Any]) -> bool:
    return any((item.get(field) or "").strip() for field in _CHARACTER_PROFILE_FIELDS)


def list_incomplete_character_names(conn) -> list[str]:
    from db import script_line_roles

    roles = script_line_roles(conn)
    names: list[str] = []
    known = {row.get("name") for row in list_characters(conn)}
    for row in list_characters(conn):
        name = (row.get("name") or "").strip()
        if not name or character_has_profile(row):
            continue
        if is_plausible_character_name(name, script_roles=roles):
            names.append(name)
    for role in roles:
        if role and role != "旁白" and role not in known:
            if is_plausible_character_name(role, script_roles=roles):
                names.append(role)
    return sorted(set(names))


def _gather_script_context_for_character(
    conn,
    name: str,
    *,
    max_chars: int = 4000,
) -> str:
    like = f"%{name}%"
    rows = conn.execute(
        """
        SELECT role, content FROM script_lines
        WHERE role = ? OR content LIKE ?
        ORDER BY chapter_num, line_idx
        """,
        (name, like),
    ).fetchall()
    parts: list[str] = []
    total = 0
    for row in rows:
        snippet = f"[{row['role']}] {row['content']}"
        if total + len(snippet) > max_chars:
            break
        parts.append(snippet)
        total += len(snippet)
    return "\n".join(parts) if parts else "（剧本中暂无相关行）"


def build_enrich_characters_prompt(
    names: list[str],
    excerpts: dict[str, str],
) -> str:
    blocks = []
    for name in names:
        blocks.append(f"### {name}\n{excerpts.get(name, '')}")
    return f"""请根据以下剧本摘录，为所列角色补全演员档案。

要求：
- 只输出一个 JSON 对象，结构同 characters_delta（可不含 parsed_lines）。
- 每个角色须含：name, gender, age, personality, quote_1, quote_2, quote_1_instruction, quote_2_instruction。
- personality 每人**不超过 {PERSONALITY_MAX_CHARS} 字**（含标点），精炼侧写，勿堆砌。
- 信息须与摘录一致，可合理推断但未出现的细节用简短描述，勿编造与原文冲突的情节。

{SCRIPT_JSON_SCHEMA_HINT}

【待补全角色及剧本摘录】
{chr(10).join(blocks)}
"""


def build_condense_personality_prompt(batch: list[dict[str, Any]]) -> str:
    payload = [
        {"name": row.get("name", ""), "personality": row.get("personality", "")}
        for row in batch
    ]
    return f"""下列角色人设超过 {PERSONALITY_MAX_CHARS} 字，请各压缩为不超过 {PERSONALITY_MAX_CHARS} 字的中文侧写。

要求：
- 只输出 JSON：{{"characters_delta": [{{"name":"…", "personality":"…"}}, …]}}
- 保留性别气质、核心性格、关键关系与剧情锚点；删除重复、同义反复与冗长列举。
- 不得添加原文没有的情节；name 须与输入完全一致。

【待压缩人设】
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def condense_overlong_personalities(
    conn,
    api_key: str,
    *,
    log: PipelineLog | None = None,
    max_chars: int = PERSONALITY_MAX_CHARS,
) -> int:
    """对超长 personality 调用模型压缩至上限以内。"""
    cap = min(max_chars, PERSONALITY_MAX_CHARS)
    sink = log or PipelineLog()
    over = [
        dict(row)
        for row in list_characters(conn)
        if len((row.get("personality") or "").strip()) > cap
    ]
    if not over:
        return 0

    condensed = 0
    batch_size = 8
    for i in range(0, len(over), batch_size):
        batch = over[i : i + batch_size]
        names = [row.get("name", "") for row in batch]
        sink.detail(f"压缩超长人设（{', '.join(names)}）")
        raw = step_chat_completion(
            api_key,
            [
                {
                    "role": "system",
                    "content": "你是 B2A-Studio 演员人设精炼工具。只输出 JSON，不要 Markdown。",
                },
                {"role": "user", "content": build_condense_personality_prompt(batch)},
            ],
            log=sink,
        )
        payload = parse_llm_json(raw)
        for item in payload.get("characters_delta") or []:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").strip()
            personality = (item.get("personality") or "").strip()
            if not name or not personality:
                continue
            if len(personality) > cap:
                personality = personality[:cap]
            upsert_character(conn, {"name": name, "personality": personality})
            condensed += 1
    if condensed:
        sink.progress(f"已压缩 {condensed} 个超长人设至 {cap} 字以内")
    return condensed


def enrich_incomplete_characters(
    conn,
    api_key: str,
    *,
    log: PipelineLog | None = None,
) -> int:
    """对仅有姓名、无人设的角色调用 LLM 补全档案。"""
    sink = log or PipelineLog()
    names = list_incomplete_character_names(conn)
    if not names:
        return 0

    excerpts = {name: _gather_script_context_for_character(conn, name) for name in names}
    sink.progress(f"补全演员人设：{', '.join(names)}")
    raw = step_chat_completion(
        api_key,
        [
            {
                "role": "system",
                "content": "你是 B2A-Studio 演员档案补全工具。只输出 JSON，不要 Markdown。",
            },
            {"role": "user", "content": build_enrich_characters_prompt(names, excerpts)},
        ],
        log=sink,
    )
    payload = parse_llm_json(raw)
    delta = payload.get("characters_delta") or []
    before = {r["name"] for r in list_characters(conn) if character_has_profile(r)}
    apply_characters_delta(conn, delta)
    condense_overlong_personalities(conn, api_key, log=sink)
    after = sum(1 for r in list_characters(conn) if character_has_profile(r))
    filled = max(0, after - len(before))
    sink.progress(f"演员人设补全完成（更新 {filled} 人）")
    return filled


_NARRATION_LINE_END = re.compile(r'[。！？…」』》"]\s*$')


def should_merge_narration_rows(prev: dict[str, Any], curr: dict[str, Any]) -> bool:
    """相邻旁白若前一条未以句末标点结束，则视为模型误拆，应合并。"""
    if (prev.get("role") or "").strip() != "旁白" or (curr.get("role") or "").strip() != "旁白":
        return False
    if prev.get("is_dialogue") or curr.get("is_dialogue"):
        return False
    prev_text = (prev.get("content") or "").strip()
    curr_text = (curr.get("content") or "").strip()
    if not prev_text or not curr_text:
        return False
    if CHAPTER_PATTERN.match(curr_text):
        return False
    return not bool(_NARRATION_LINE_END.search(prev_text))


def merge_fragmented_narration_lines(
    lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """合并模型误拆的连续旁白行（含词语中间断开）。"""
    if not lines:
        return lines
    merged: list[dict[str, Any]] = []
    buf: dict[str, Any] | None = None
    for row in lines:
        if buf is None:
            buf = dict(row)
            continue
        if should_merge_narration_rows(buf, row):
            buf["content"] = (buf.get("content") or "") + (row.get("content") or "")
            if not (buf.get("emotion_instruction") or "").strip():
                buf["emotion_instruction"] = row.get("emotion_instruction") or ""
        else:
            merged.append(buf)
            buf = dict(row)
    if buf is not None:
        merged.append(buf)
    return merged


_SPEECH_VERB_TAIL = (
    r"(?:说道|说|问道|问|答道|答|喊道|叫道|劝道|笑道|沉声道|轻声道|开口道|"
    r"应声|回了句|嘟囔|骂道|吼道|回答|顺势劝道|开口|叮嘱|提醒|解释|继续|接着)"
)

_SPEECH_GUIDE_PREFIX_RE = re.compile(
    rf"^[\u4e00-\u9fffA-Za-z0-9·]{{1,12}}{_SPEECH_VERB_TAIL}[:：，,]?\s*"
)

_PREFIX_NARRATION_DIALOGUE_RE = re.compile(
    rf"^(.+?{_SPEECH_VERB_TAIL}[:：，,]\s*)(.+)$",
    re.DOTALL,
)

_TRAILING_SPEAKER_TAG_RE = re.compile(
    rf"([，,、]?\s*)[\u4e00-\u9fffA-Za-z0-9·]{{1,12}}{_SPEECH_VERB_TAIL}\s*[。．.!！?？]?\s*$"
)

_SANDWICH_DIALOGUE_RE = re.compile(
    r"^[\s]*[“\"「『](.+?)[”\"」』]\s*(.+?)\s*[“\"「『](.+?)[”\"」』]\s*$",
    re.DOTALL,
)

_QUOTE_IN_TEXT_RE = re.compile(r"[“\"「『]")

_QUOTE_WRAP_PAIRS = (
    ("「", "」"),
    ("『", "』"),
    ('"', '"'),
    ("'", "'"),
)


def _strip_wrapping_quotes(text: str) -> str:
    t = text.strip()
    for left, right in _QUOTE_WRAP_PAIRS:
        if (
            t.startswith(left)
            and t.endswith(right)
            and len(t) > len(left) + len(right)
        ):
            inner = t[len(left) : -len(right)].strip()
            if inner:
                t = inner
    return t


def _narration_row(
    row: dict[str, Any],
    content: str,
    *,
    emotion: str | None = None,
) -> dict[str, Any]:
    return {
        "role": "旁白",
        "emotion_instruction": emotion
        if emotion is not None
        else (row.get("emotion_instruction") or ""),
        "content": content,
        "is_dialogue": False,
        "voice_id": (row.get("voice_id") or "").strip(),
    }


def _dialogue_row(row: dict[str, Any], content: str) -> dict[str, Any]:
    out = dict(row)
    out["is_dialogue"] = True
    out["content"] = _strip_wrapping_quotes(content.strip())
    return out


def _likely_narration_prefix(prefix: str) -> bool:
    prefix = prefix.strip()
    if not prefix or _QUOTE_IN_TEXT_RE.search(prefix):
        return False
    return bool(re.search(rf"{_SPEECH_VERB_TAIL}[:：，,]\s*$", prefix))


def split_misclassified_dialogue_rows(row: dict[str, Any]) -> list[dict[str, Any]]:
    """后处理：尝试修复三大失败模态（夹心/前置引导/后置引导）。"""
    if not row.get("is_dialogue"):
        return [row]
    role = (row.get("role") or "").strip() or "旁白"
    if role == "旁白":
        return [{**row, "role": "旁白", "is_dialogue": False}]

    content = (row.get("content") or "").strip()
    if not content:
        return [row]

    emotion = row.get("emotion_instruction") or ""

    m = _SANDWICH_DIALOGUE_RE.match(content)
    if m:
        q1, middle, q2 = m.groups()
        if middle.strip() and not _QUOTE_IN_TEXT_RE.search(middle):
            return [
                _dialogue_row(row, q1),
                _narration_row(row, middle.strip(), emotion=emotion),
                _dialogue_row(row, q2),
            ]

    m = _TRAILING_SPEAKER_TAG_RE.search(content)
    if m:
        dialogue = _strip_wrapping_quotes(content[: m.start()].strip())
        suffix = content[m.start() :].strip()
        if dialogue and suffix:
            return [
                _dialogue_row(row, dialogue),
                _narration_row(row, suffix, emotion="平实交代说话主语"),
            ]

    m = _PREFIX_NARRATION_DIALOGUE_RE.match(content)
    if m:
        prefix, rest = m.groups()
        if _likely_narration_prefix(prefix):
            dialogue = _strip_wrapping_quotes(rest.strip())
            if dialogue and prefix.strip():
                return [
                    _narration_row(row, prefix.strip(), emotion="平缓叙述动作"),
                    _dialogue_row({**row, "role": role}, dialogue),
                ]

    return [row]


def coerce_dialogue_isolation(row: dict[str, Any]) -> dict[str, Any]:
    """后处理：剥离对白行中外层引号与「某某说/劝道」类引导语。"""
    row = dict(row)
    content = (row.get("content") or "").strip()
    if not content or not row.get("is_dialogue"):
        return row
    content = _strip_wrapping_quotes(content)
    for _ in range(4):
        m = _SPEECH_GUIDE_PREFIX_RE.match(content)
        if not m:
            break
        rest = content[m.end() :].strip()
        if not rest or rest == content:
            break
        content = _strip_wrapping_quotes(rest)
    m = _TRAILING_SPEAKER_TAG_RE.search(content)
    if m:
        content = _strip_wrapping_quotes(content[: m.start()].strip())
    row["content"] = content
    return row


def normalize_parsed_line(row: dict[str, Any]) -> dict[str, Any] | None:
    content = (row.get("content") or row.get("text") or "").strip()
    if not content:
        return None
    role = (row.get("role") or row.get("人物") or "旁白").strip() or "旁白"
    emotion = (row.get("emotion_instruction") or row.get("emotion") or "").strip()
    is_dialogue = bool(row.get("is_dialogue"))
    if "is_dialogue" not in row and role != "旁白":
        is_dialogue = "“" in content or '"' in content
    return {
        "role": role,
        "emotion_instruction": emotion,
        "content": content,
        "is_dialogue": is_dialogue,
        "voice_id": (row.get("voice_id") or "").strip(),
    }


# 常见单姓（用于判断具名角色，避免把「月亮」「打听」等写入演员表）
_COMMON_SURNAMES = frozenset(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹"
    "柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤"
    "滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄"
    "米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭"
    "梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣邓"
    "郁单杭洪包诸左石崔吉龚程邢裴陆荣翁荀羊惠甄曲封芮羿储靳汲邴糜松井段富巫"
    "乌焦巴弓牧隗车侯宓蓬全郗班仰秋仲伊宫宁仇栾甘戎祖武符刘景詹束龙叶司郜"
    "黎蓟薄印宿白怀蒲台从鄂索咸籍赖卓屠蒙池乔阴胥能苍双闻莘党翟谭贡逄姬冉"
    "郦雍璩桑桂濮牛寿通边燕冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古"
    "易慎戈廖庾终暨居衡步都耿满弘匡文寇广禄阙东殴沃利蔚越夔隆师巩厍聂晁"
    "敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓"
)

_INVALID_CHARACTER_NAMES = frozenset(
    {
        "自己",
        "他们",
        "我们",
        "对方",
        "众人",
        "那人",
        "此人",
        "什么",
        "如何",
        "为何",
        "一个",
        "这位",
        "那位",
        "心里",
        "眼里",
        "到了",
        "打听",
        "月亮",
        "相机",
        "个人肯定",
        "这个行",
        "老板好友",
    }
)

_CJK_NAME_RE = re.compile(r"^[\u4e00-\u9fff]{2,6}$")
_ROLE_TITLE_SUFFIXES = ("老板", "老板娘", "医生", "护士", "师傅", "同学", "老师")


def is_plausible_character_name(
    name: str,
    *,
    script_roles: set[str] | None = None,
) -> bool:
    """过滤叙述误抽的非人名词条；剧本对白 role 与职业称呼始终保留。"""
    name = (name or "").strip()
    if not name or name == "旁白":
        return False
    if script_roles and name in script_roles:
        return True
    if name in _INVALID_CHARACTER_NAMES:
        return False
    if any(ch in name for ch in "*#@/\\|<>{}[]"):
        return False
    if not _CJK_NAME_RE.fullmatch(name):
        return False
    if name.startswith(("这", "那", "某", "各", "每", "该", "此")):
        return False
    if name.endswith(("了", "着", "过", "吗", "呢", "吧", "啊", "嘛")):
        return False
    if name[0] in "到去打听望向见问说想在用有被让给跟从":
        return False
    if any(name.endswith(suffix) for suffix in _ROLE_TITLE_SUFFIXES):
        return True
    if name in {"老板", "老板娘"}:
        return True
    if name[0] in _COMMON_SURNAMES:
        return True
    return False


def character_mentioned_in_script(conn, name: str) -> bool:
    like = f"%{name}%"
    row = conn.execute(
        """
        SELECT 1 FROM script_lines
        WHERE role = ? OR content LIKE ?
        LIMIT 1
        """,
        (name, like),
    ).fetchone()
    return row is not None


def is_kept_character(
    name: str,
    row: dict[str, Any],
    script_roles: set[str],
    *,
    mentioned_in_script: bool = False,
) -> bool:
    """演员表保留：有对白、有人设、或剧本中出现且待补全的具名角色。"""
    name = (name or "").strip()
    if name in script_roles:
        return is_plausible_character_name(name, script_roles=script_roles)
    if not is_plausible_character_name(name, script_roles=script_roles):
        return False
    if character_has_profile(row):
        return True
    if (row.get("age") or "").strip() or (row.get("quote_1") or "").strip():
        return True
    if mentioned_in_script:
        return True
    return False


def apply_characters_delta(
    conn,
    delta: list[Any],
    *,
    script_roles: set[str] | None = None,
) -> None:
    from db import script_line_roles

    roles = script_roles if script_roles is not None else script_line_roles(conn)
    for item in delta:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not is_plausible_character_name(name, script_roles=roles):
            continue
        if not delta_has_profile(item):
            continue
        upsert_character(conn, item)


def sync_speaking_roles_to_cast(
    conn,
    parsed_lines: list[Any] | None = None,
) -> int:
    """将剧本中对白 role 同步进演员表（含民宿老板等职业称呼，后续可补人设）。"""
    from db import script_line_roles

    roles = script_line_roles(conn)
    if parsed_lines:
        for item in parsed_lines:
            if isinstance(item, dict):
                role = (item.get("role") or "").strip()
                if role and role != "旁白":
                    roles.add(role)

    known = {row["name"] for row in list_characters(conn) if row.get("name")}
    added = 0
    for role in sorted(roles):
        if not role or role == "旁白" or role in known:
            continue
        if not is_plausible_character_name(role, script_roles=roles):
            continue
        upsert_character(conn, {"name": role})
        known.add(role)
        added += 1
    return added


def ensure_characters_from_script(
    conn,
    parsed_lines: list[Any],
    characters_delta: list[Any],
    *,
    api_key: str | None = None,
    log: PipelineLog | None = None,
    run_condense: bool = True,
) -> int:
    """合并模型角色表并同步本段对白角色到演员表。"""
    from db import script_line_roles

    roles = script_line_roles(conn)
    for item in parsed_lines:
        if isinstance(item, dict):
            role = (item.get("role") or "").strip()
            if role and role != "旁白":
                roles.add(role)
    apply_characters_delta(conn, characters_delta or [], script_roles=roles)
    if api_key and run_condense:
        condense_overlong_personalities(conn, api_key, log=log)
    return sync_speaking_roles_to_cast(conn, parsed_lines)


def prune_spurious_characters(conn) -> int:
    """删除误写入演员表的词组/碎片。"""
    from db import delete_characters_by_names, script_line_roles

    script_roles = script_line_roles(conn)
    to_delete: list[str] = []
    for row in list_characters(conn):
        name = (row.get("name") or "").strip()
        mentioned = character_mentioned_in_script(conn, name)
        if not is_kept_character(
            name, row, script_roles, mentioned_in_script=mentioned
        ):
            to_delete.append(name)
    return delete_characters_by_names(conn, to_delete)


def _chapter_target_text(chapter: NovelChapter) -> str:
    if chapter.overlap_prefix > 0:
        return chapter.text[chapter.overlap_prefix :]
    return chapter.text


def text_without_punctuation(text: str) -> str:
    """对比覆盖率用：去掉空白与 Unicode 标点（中英文标点均剔除）。"""
    return "".join(
        ch
        for ch in text
        if not ch.isspace() and not unicodedata.category(ch).startswith("P")
    )


def compare_text_coverage(source: str, script: str) -> dict[str, int | float]:
    """
    对比原文与剧本正文的字数（去标点）。
    返回 source_no_punct、script_no_punct、ratio，及含标点的 raw 字数供展示。
    """
    src_cmp = text_without_punctuation(source or "")
    scr_cmp = text_without_punctuation(script or "")
    src_n = len(src_cmp)
    scr_n = len(scr_cmp)
    ratio = (scr_n / src_n) if src_n > 0 else 1.0
    return {
        "source_no_punct": src_n,
        "script_no_punct": scr_n,
        "source_raw": len((source or "").strip()),
        "script_raw": len((script or "").strip()),
        "ratio": ratio,
    }


def chapter_parse_coverage(chapter: NovelChapter, parsed_lines: list[Any]) -> float:
    """本章剧本行相对原文的覆盖率（去标点字数比）。"""
    target = _chapter_target_text(chapter).strip()
    if not target:
        return 1.0
    combined = ""
    for item in parsed_lines:
        if isinstance(item, dict):
            combined += str(item.get("content") or item.get("text") or "")
    if not combined.strip():
        return 0.0
    return float(compare_text_coverage(target, combined)["ratio"])


def audit_chapter_script_duplicates(
    conn: Any,
    chapter_num: int,
    *,
    min_len: int = 12,
) -> dict[str, Any]:
    """
    检测一章剧本中重复 content（去标点后完全相同）。
    返回重复组、估计重复字数占比，供覆盖率>100% 时排查。
    """
    from collections import defaultdict

    rows = conn.execute(
        """
        SELECT line_idx, content FROM script_lines
        WHERE chapter_num = ?
        ORDER BY line_idx
        """,
        (chapter_num,),
    ).fetchall()
    line_by_idx: dict[int, str] = {
        int(r[0]): str(r[1] or "") for r in rows
    }
    ordered_idxs = sorted(line_by_idx)
    total_chars = sum(
        len(text_without_punctuation(line_by_idx[i])) for i in ordered_idxs
    )

    buckets: dict[str, list[int]] = defaultdict(list)
    for idx in ordered_idxs:
        key = text_without_punctuation(line_by_idx[idx])
        if len(key) < min_len:
            continue
        buckets[key].append(idx)

    groups: list[dict[str, Any]] = []
    duplicate_chars = 0
    for key, line_idxs in buckets.items():
        if len(line_idxs) < 2:
            continue
        extra = len(line_idxs) - 1
        duplicate_chars += len(key) * extra
        preview = line_by_idx[line_idxs[0]]
        groups.append(
            {
                "line_idxs": line_idxs,
                "repeat_count": len(line_idxs),
                "preview": preview[:100] + ("…" if len(preview) > 100 else ""),
                "extra_chars_no_punct": len(key) * extra,
            }
        )
    groups.sort(key=lambda g: -int(g["extra_chars_no_punct"]))

    consecutive: list[int] = []
    prev_key = ""
    for idx in ordered_idxs:
        key = text_without_punctuation(line_by_idx[idx])
        if key and key == prev_key and len(key) >= min_len:
            consecutive.append(idx)
        prev_key = key

    return {
        "chapter_num": chapter_num,
        "line_count": len(ordered_idxs),
        "total_chars_no_punct": total_chars,
        "duplicate_group_count": len(groups),
        "duplicate_char_estimate": duplicate_chars,
        "duplicate_ratio": (
            (duplicate_chars / total_chars) if total_chars > 0 else 0.0
        ),
        "groups": groups[:25],
        "consecutive_duplicate_line_idxs": consecutive[:40],
    }


def compute_novel_chapter_coverage_report(
    novel_text: str,
    conn: Any,
) -> list[dict[str, Any]]:
    """按章对比 SQLite 剧本行与原文章节（去标点字数）。"""
    from db import fetch_chapter_script_content

    chapters = slice_novel_by_chapters(novel_text)
    report: list[dict[str, Any]] = []
    for chapter in chapters:
        source = _chapter_target_text(chapter).strip()
        script = fetch_chapter_script_content(conn, chapter.chapter_num)
        stats = compare_text_coverage(source, script)
        report.append(
            {
                "chapter_num": chapter.chapter_num,
                "chapter_index": chapter.index,
                "slice_mode": chapter.slice_mode,
                **stats,
            }
        )
    return report


def build_chapter_coverage_retry_prompt(chapter: NovelChapter) -> str:
    """轻量补全提示，避免重复嵌套整章原文导致 500。"""
    preamble_hint = ""
    if chapter.preamble_chars > 0:
        preamble_hint = (
            f"- 本章开头约 {chapter.preamble_chars} 字为书前信息（「第一章」标题之前），"
            "也须逐字进入 parsed_lines。\n"
        )
    return f"""上次输出的 parsed_lines 行数过少。请重新输出一个 JSON 对象。

硬性要求：
- parsed_lines 须覆盖下方本章原文的每一段（含书前信息与章标题后的全部叙述与对白）。
{preamble_hint}- 不得只输出书名、章题或一行概述；content 与原文逐字一致（含标点）。
- 只输出 JSON，不要 Markdown，不要长段分析。

{SCRIPT_BLOCK_FORMAT_HINT}

【本章原文】
{chapter.text}
"""


def expand_parsed_lines_in_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """从 payload 全树收集剧本行，修复模型只返回单行/单行挂在顶层的情况。"""
    existing = payload.get("parsed_lines")
    if not isinstance(existing, list):
        existing = []
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_row(row: dict[str, Any]) -> None:
        normalized = normalize_parsed_line(row)
        if not normalized:
            return
        key = normalized["content"]
        if key in seen:
            return
        seen.add(key)
        collected.append(normalized)

    for item in existing:
        if isinstance(item, dict):
            add_row(item)

    if len(collected) < 3:

        def walk(obj: Any) -> None:
            if isinstance(obj, dict):
                if _looks_like_line_row(obj) and not (obj.get("name") or "").strip():
                    add_row(_line_row_from_dict(obj))
                for value in obj.values():
                    walk(value)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)

        walk(payload)

    if len(collected) > len(existing):
        payload["parsed_lines"] = collected
    return payload


CHAPTER_COVERAGE_MIN = float(os.environ.get("B2A_CHAPTER_COVERAGE_MIN", "0.98"))
CHAPTER_COVERAGE_MAX = float(os.environ.get("B2A_CHAPTER_COVERAGE_MAX", "1.001"))
PIPELINE_FAILURE_RETRY_WAIT_SEC = int(
    os.environ.get("B2A_PIPELINE_RETRY_WAIT", "600")
)
MAX_COVERAGE_FIXUP_ROUNDS = int(
    os.environ.get("B2A_MAX_COVERAGE_FIXUP_ROUNDS", "100")
)


def chapter_coverage_acceptable(ratio: float) -> bool:
    return CHAPTER_COVERAGE_MIN <= ratio <= CHAPTER_COVERAGE_MAX


def list_chapters_with_coverage_out_of_range(
    novel_text: str,
    conn: Any | None = None,
) -> list[int]:
    """返回已有剧本但覆盖率 <98% 或 >100.1% 的章节号。"""
    if conn is not None:
        return _list_chapters_with_coverage_out_of_range_conn(novel_text, conn)
    ensure_database()
    with get_connection() as conn:
        return _list_chapters_with_coverage_out_of_range_conn(novel_text, conn)


def _list_chapters_with_coverage_out_of_range_conn(
    novel_text: str,
    conn: Any,
) -> list[int]:
    report = compute_novel_chapter_coverage_report(novel_text, conn)
    count_rows = conn.execute(
        """
        SELECT chapter_num, COUNT(*) AS cnt
        FROM script_lines
        GROUP BY chapter_num
        """
    ).fetchall()
    line_counts = {int(r["chapter_num"]): int(r["cnt"]) for r in count_rows}
    bad: list[int] = []
    for row in report:
        ch = int(row["chapter_num"])
        if line_counts.get(ch, 0) <= 0:
            continue
        ratio = float(row["ratio"])
        if not chapter_coverage_acceptable(ratio):
            bad.append(ch)
    return sorted(bad)


def chapter_output_too_short(
    chapter: NovelChapter,
    parsed_lines: list[Any],
    coverage: float,
) -> bool:
    target_len = len(_chapter_target_text(chapter).strip())
    if target_len < 120:
        return len(parsed_lines) < 1
    if coverage < CHAPTER_COVERAGE_MIN:
        return True
    return len(parsed_lines) < 3 and coverage < 0.25


def apply_chapter_parsed_lines(
    conn,
    parsed_lines: list[Any],
    *,
    chapter_num: int,
    chapter_subtitles: dict[int, str] | None = None,
) -> int:
    """将一章的 parsed_lines 写入库（覆盖该章已有剧本行）。"""
    from utils.chapter_title_lines import split_merged_chapter_opening_rows

    normalized: list[dict[str, Any]] = []
    for item in parsed_lines:
        if not isinstance(item, dict):
            continue
        row = normalize_parsed_line(item)
        if not row:
            continue
        for part in split_misclassified_dialogue_rows(row):
            normalized.append(coerce_dialogue_isolation(part))
    normalized = merge_fragmented_narration_lines(normalized)
    titles = chapter_subtitles or {}
    normalized = split_merged_chapter_opening_rows(
        normalized, int(chapter_num), titles
    )

    conn.execute(
        "DELETE FROM script_lines WHERE chapter_num = ?",
        (chapter_num,),
    )
    if not normalized:
        return 0

    insert_script_lines(
        conn,
        normalized,
        chapter_num=chapter_num,
        start_line_idx=1,
    )
    return len(normalized)


def compact_fragmented_script_lines(conn) -> int:
    """整理库内已写入的误拆旁白（按章合并并重排 line_idx）。"""
    chapters = [
        int(r[0])
        for r in conn.execute(
            "SELECT DISTINCT chapter_num FROM script_lines ORDER BY chapter_num"
        ).fetchall()
    ]
    removed = 0
    for chapter_num in chapters:
        rows = [
            {
                "role": r["role"],
                "emotion_instruction": r["emotion_instruction"],
                "content": r["content"],
                "is_dialogue": bool(r["is_dialogue"]),
                "voice_id": r["voice_id"] or "",
            }
            for r in conn.execute(
                """
                SELECT role, emotion_instruction, content, is_dialogue, voice_id
                FROM script_lines
                WHERE chapter_num = ?
                ORDER BY line_idx
                """,
                (chapter_num,),
            ).fetchall()
        ]
        compacted = merge_fragmented_narration_lines(rows)
        if len(compacted) >= len(rows):
            continue
        conn.execute(
            "DELETE FROM script_lines WHERE chapter_num = ?",
            (chapter_num,),
        )
        insert_script_lines(
            conn,
            compacted,
            chapter_num=chapter_num,
            start_line_idx=1,
        )
        removed += len(rows) - len(compacted)
    return removed


def process_novel_pipeline(
    novel_text: str,
    api_key: str,
    *,
    reset_db: bool = True,
    resume: bool = False,
    reprocess_chapter_nums: list[int] | None = None,
    novel_name: str = "",
    on_progress: ProgressCallback | None = None,
    on_log: LogCallback | None = None,
) -> PipelineResult:
    """
    按章拆解：每章完整原文一次 LLM 调用，链式人设记忆 → SQLite。

    Args:
        novel_text: Full novel plain text.
        api_key: StepPlan API key.
        reset_db: If True, wipe DB and checkpoints before processing.
        resume: If True, skip chapters already in pipeline_checkpoints (implies reset_db=False).
        reprocess_chapter_nums: 仅重跑所列「章节号」；清除其检查点后覆盖写入（不清全书库）。
        novel_name: Used with text for checkpoint fingerprint.
        on_progress: Optional callback(completed_steps, total_steps, message).
    """
    if not novel_text.strip():
        raise ValueError("小说文本为空，无法拆解。")
    if not api_key.strip():
        raise ValueError("未配置 Step API Key。")
    if resume and reset_db:
        raise ValueError("断点续跑不能与清空数据库同时使用。")
    if reprocess_chapter_nums and reset_db:
        raise ValueError("指定章节重跑不能与清空数据库同时使用。")
    if reprocess_chapter_nums and resume:
        raise ValueError("指定章节重跑请单独使用，不要与断点续跑同时触发。")

    reprocess_set: frozenset[int] | None = (
        frozenset(int(n) for n in reprocess_chapter_nums)
        if reprocess_chapter_nums
        else None
    )

    plog = PipelineLog(on_line=on_log)
    from utils.chapter_title_lines import build_chapter_subtitles_map

    chapter_subtitles = build_chapter_subtitles_map(novel_text)
    chapters = slice_novel_by_chapters(novel_text)
    if not chapters:
        raise ValueError("未能切分小说正文，请检查上传文件。")

    if reprocess_set is not None:
        chapters_to_run = [ch for ch in chapters if ch.chapter_num in reprocess_set]
        missing = sorted(reprocess_set - {ch.chapter_num for ch in chapters_to_run})
        if missing:
            raise ValueError(f"书中未找到章节号：{missing}")
        if not chapters_to_run:
            raise ValueError("未选择有效章节。")
    else:
        chapters_to_run = chapters

    total = len(chapters_to_run)
    uses_fixed_slices = chapters[0].slice_mode == "fixed"
    if reprocess_set is not None:
        labels = ", ".join(f"第 {ch.chapter_num} 章" for ch in chapters_to_run)
        plog.progress(f"指定章节重跑 · {labels}（共 {total} 个任务）")
    elif uses_fixed_slices:
        plog.progress(
            f"开始拆解 · 全书 {len(novel_text)} 字 · 未检测到「第N章」，"
            f"按 {FALLBACK_CHAPTER_SIZE} 字/段（重叠 {FALLBACK_CHAPTER_OVERLAP} 字）"
            f"共 {total} 段"
        )
    else:
        plog.progress(f"开始按章拆解 · 全书 {len(novel_text)} 字 · 共 {total} 章")
    plog.detail(
        f"模型 {LLM_MODEL} · {step_plan_pipeline_endpoint()} · "
        f"max_tokens={_format_max_tokens_label(resolve_chapter_max_tokens())}"
    )

    novel_fp = compute_novel_fingerprint(novel_text, novel_name)
    chunk_cfg = chunk_config_signature()

    if reset_db:
        reset_database()
        with get_connection() as conn:
            clear_checkpoints(conn, novel_fp)
        plog.progress("已清空剧本库，从头拆解")
    else:
        ensure_database()

    chapters_skipped = 0

    if reprocess_set is not None:
        with get_connection() as conn:
            for ch in chapters_to_run:
                clear_chunk_checkpoint(conn, novel_fp, ch.index)
                clear_blocked_segments_for_chapter(
                    conn, novel_fp, ch.chapter_num
                )
            conn.commit()
        plog.detail(
            "已清除选中章的检查点与待手动录入记录；"
            "写入时将先删除该章旧剧本行再插入新结果。"
        )
        completed: set[int] = set()
    else:
        with get_connection() as conn:
            completed = (
                _merge_completed_chunk_indices(
                    conn, novel_text, novel_name, chunk_cfg
                )
                if resume
                else set()
            )

    if reprocess_set is not None:
        if on_progress:
            on_progress(
                0,
                total,
                f"重跑 **{len(chapters_to_run)}** 章："
                + "、".join(f"第 {ch.chapter_num} 章" for ch in chapters_to_run[:8])
                + ("…" if len(chapters_to_run) > 8 else ""),
            )
    elif resume:
        pending_chapters = [ch for ch in chapters if ch.index not in completed]
        if pending_chapters:
            nxt = pending_chapters[0]
            next_label = (
                f"小说第 {nxt.chapter_num} 章（第 {nxt.index}/{total} 个拆解任务）"
            )
        else:
            next_label = "无（全部已完成）"
        plog.progress(
            f"断点续跑：已完整完成 {len(completed)}/{total} 个任务，从 {next_label} 继续"
        )
        plog.detail(f"已跳过任务序: {sorted(completed)}")
        plog.detail(
            "说明：仅跳过「整章已跑完并写入检查点」的任务；"
            "上次中断时正在跑的那一章会重新请求 API。"
        )
        if on_progress and pending_chapters:
            on_progress(
                len(completed),
                total,
                f"断点续跑 · 已完成 **{len(completed)}/{total}** 个任务 · "
                f"下一章：**第 {pending_chapters[0].chapter_num}** 章",
            )
    elif not reset_db:
        plog.detail("保留现有剧本库")

    for run_idx, chapter in enumerate(chapters_to_run, start=1):
        range_label = (
            f"第 {chapter.chapter_num} 章 · 字符 {chapter.start}–{chapter.end} · "
            f"{len(chapter.text)} 字"
        )

        with get_connection() as conn:
            if (
                reprocess_set is None
                and resume
                and chapter.index in completed
            ):
                if not is_chunk_checkpoint_valid_for_novel(
                    conn,
                    novel_text,
                    novel_name,
                    chapter.index,
                    chunk_cfg,
                    chapter.start,
                    chapter.end,
                ):
                    plog.warn(
                        f"第 {chapter.chapter_num} 章检查点与当前切分不一致，将重新拆解"
                    )
                else:
                    chapters_skipped += 1
                    plog.progress(
                        f"已跳过 · 小说第 {chapter.chapter_num} 章"
                        f"（任务 {chapter.index}/{total}，检查点已存在）"
                    )
                    if on_progress:
                        on_progress(
                            chapter.index,
                            total,
                            f"断点续跑 · 已跳过 **第 {chapter.chapter_num}** 章"
                            f"（任务 {chapter.index}/{total}）",
                        )
                    continue

            memory = build_memory_block(list_characters(conn))
            incomplete = list_incomplete_character_names(conn)

        if len(chapter.text) > CHAPTER_WARN_CHARS:
            plog.warn(
                f"第 {chapter.chapter_num} 章约 {len(chapter.text)} 字，"
                "较长，若 API 超时或 JSON 截断可考虑拆分章节或提高 max_tokens"
            )

        step_pos = run_idx if reprocess_set is not None else chapter.index
        plog.progress(
            f"第 {chapter.chapter_num} 章（{step_pos}/{total}）"
            f"：调用模型中（每 15 秒刷新；"
            f"连续 {CHAPTER_READ_TIMEOUT_SEC // 60} 分钟无新数据将超时）…"
        )
        plog.detail(
            f"本章 API：max_tokens={_format_max_tokens_label(resolve_chapter_max_tokens())}；"
            f">{CHAPTER_SINGLE_SHOT_MAX} 字将拆段；"
            f"read_timeout={CHAPTER_READ_TIMEOUT_SEC}s"
        )
        plog.detail(
            f"第 {chapter.chapter_num} 章原文 {len(chapter.text)} 字；"
            "已释放数据库连接，避免与页面预览争锁"
        )
        if chapter.preamble_chars > 0:
            plog.progress(
                f"第 {chapter.chapter_num} 章含书前信息约 {chapter.preamble_chars} 字"
                f"（书名/前言，须一并拆解）"
            )
            plog.detail(
                f"书前信息区间：[0, {chapter.preamble_chars})；"
                f"第1章范围：全书开头至「第二章」标记前"
            )

        if on_progress:
            on_progress(
                run_idx - 1,
                total,
                f"正在分析第 **{chapter.chapter_num}** 章"
                f"（{run_idx}/{total} · 任务序 {chapter.index}）"
                f" · {len(chapter.text)} 字 · 请求模型中…",
            )

        t_api = time.time()
        from utils.sensitive_content import llm_parse_chapter_with_sensitive_split

        payload, blocked_segments = llm_parse_chapter_with_sensitive_split(
            api_key,
            chapter,
            total,
            memory,
            incomplete_names=incomplete or None,
            log=plog,
        )
        if blocked_segments:
            plog.warn(
                f"第 {chapter.chapter_num} 章：{len(blocked_segments)} 段原文"
                "因审核无法自动拆解，已记入待手动录入列表（可在剧本预览区处理）。"
            )
        payload, parsed, refine_blocked = refine_chapter_payload_after_parse(
            api_key,
            chapter,
            total,
            memory,
            payload,
            incomplete_names=incomplete or None,
            log=plog,
        )
        if refine_blocked:
            blocked_segments = list(blocked_segments) + list(refine_blocked)
        api_sec = time.time() - t_api
        n_chars_delta = len(payload.get("characters_delta") or [])
        plog.progress(
            f"第 {chapter.chapter_num} 章模型返回完成（{len(parsed)} 行），"
            "正在写入数据库…"
        )

        with get_connection() as conn:
            extra_chars = ensure_characters_from_script(
                conn,
                parsed,
                payload.get("characters_delta") or [],
                api_key=None,
                log=plog,
                run_condense=False,
            )
            lines_written = apply_chapter_parsed_lines(
                conn,
                parsed,
                chapter_num=chapter.chapter_num,
                chapter_subtitles=chapter_subtitles,
            )
            if blocked_segments:
                replace_blocked_segments_for_chapter(
                    conn,
                    novel_fp,
                    chapter.chapter_num,
                    [
                        {
                            "char_start": seg.char_start,
                            "char_end": seg.char_end,
                            "snippet": seg.snippet,
                            "reason": seg.reason,
                        }
                        for seg in blocked_segments
                    ],
                )
            sync_speaking_roles_to_cast(conn, parsed)
            rank_stats = refresh_rolling_character_ranks(conn)
            conn.commit()

            mark_chunk_completed(
                conn,
                novel_fingerprint=novel_fp,
                chunk_index=chapter.index,
                chunk_config=chunk_cfg,
                char_start=chapter.start,
                char_end=chapter.end,
                script_lines_added=lines_written,
            )
            conn.commit()

        if api_key:
            try:
                with get_connection() as conn:
                    condense_overlong_personalities(conn, api_key, log=plog)
                    conn.commit()
            except Exception as exc:
                plog.warn(
                    f"第 {chapter.chapter_num} 章人设压缩跳过（不影响剧本写入）: {exc}"
                )

        extra_note = f"，补登记演员 {extra_chars} 人" if extra_chars else ""
        plog.progress(
            f"第 {chapter.chapter_num} 章完成（{api_sec:.0f}s）"
            f"：写入剧本行 {lines_written} 条"
            f"，角色更新 {n_chars_delta} 条{extra_note}；"
            f"Top {ROLLING_RANK_TOP_N} 演员榜已重排"
            f"（main {rank_stats['main']} · extra {rank_stats['extra']}）"
        )
        plog.detail(range_label)

        if on_progress:
            on_progress(
                run_idx,
                total,
                f"第 **{chapter.chapter_num}** 章已完成 · {lines_written} 行 · "
                f"正在流式深化链式人设记忆并重排演员榜"
                f"（Top {ROLLING_RANK_TOP_N}：main {rank_stats['main']}）…",
            )

    if reprocess_set is None:
        coverage_round = 0
        while coverage_round < MAX_COVERAGE_FIXUP_ROUNDS:
            bad_chapters = list_chapters_with_coverage_out_of_range(novel_text)
            if not bad_chapters:
                break
            coverage_round += 1
            detail = "、".join(
                f"第{n}章" for n in bad_chapters[:20]
            )
            if len(bad_chapters) > 20:
                detail += f"…（共 {len(bad_chapters)} 章）"
            plog.progress(
                f"覆盖率补跑第 {coverage_round} 轮 · "
                f"未达标（<{CHAPTER_COVERAGE_MIN:.0%} 或 >{CHAPTER_COVERAGE_MAX:.1%}）："
                f"{detail}"
            )
            process_novel_pipeline(
                novel_text,
                api_key,
                reset_db=False,
                resume=False,
                reprocess_chapter_nums=bad_chapters,
                novel_name=novel_name,
                on_progress=on_progress,
                on_log=on_log,
            )
        if coverage_round >= MAX_COVERAGE_FIXUP_ROUNDS:
            remaining = list_chapters_with_coverage_out_of_range(novel_text)
            if remaining:
                plog.warn(
                    f"覆盖率补跑已达 {MAX_COVERAGE_FIXUP_ROUNDS} 轮上限，"
                    f"仍有 {len(remaining)} 章未达标，请在剧本预览区手动指定重跑。"
                )

    with get_connection() as conn:
        still_incomplete = list_incomplete_character_names(conn)
        if still_incomplete:
            try:
                enrich_incomplete_characters(conn, api_key, log=plog)
                conn.commit()
            except Exception as exc:
                plog.warn(f"演员人设自动补全失败（可稍后在预览区手动补全）: {exc}")

        refresh_rolling_character_ranks(conn)
        try:
            condense_overlong_personalities(conn, api_key, log=plog)
        except Exception as exc:
            plog.warn(f"全书人设压缩失败（可稍后在预览区补全/重跑）: {exc}")
        conn.commit()
        stats = get_pipeline_stats(conn)
        importance = get_importance_stats(conn)
    plog.progress(
        f"拆解完成：演员 {stats['characters']} 人，"
        f"剧本行 {stats['script_lines']} 条，"
        f"覆盖 {stats['chapters']} 章"
        + (f"（跳过 {chapters_skipped} 章）" if chapters_skipped else "")
    )

    return PipelineResult(
        chunks_total=total,
        book_chapters_total=len(chapters),
        characters=stats["characters"],
        script_lines=stats["script_lines"],
        chapters=stats["chapters"],
        chunks_skipped=chapters_skipped,
        main_characters=importance["main"],
        extra_characters=importance["extra"],
        pending_characters=importance["pending"],
        reprocess_only=reprocess_set is not None,
    )
