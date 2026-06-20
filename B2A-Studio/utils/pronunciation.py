"""读音校正：剧本本地扫描、最长匹配去重、StepAudio pronunciation_map 规则。"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import jieba
from pypinyin import Style, pinyin

from db import (
    PRONUNCIATION_STATUS_CONFIRMED,
    PRONUNCIATION_STATUS_IGNORED,
    PRONUNCIATION_STATUS_PENDING,
    list_characters,
    list_confirmed_pronunciation_rules,
    list_pronunciation_rules,
    upsert_pronunciation_rule,
)

# TTS 易读错的多音字（不含「了/地/和/会」等常见字，避免误扫碎片与人名）
_TTS_RISKY_CHARS = frozenset(
    "调重长朝降血差发结弹乐藏率露契觉积量度传当将更曾便差降薄宁胜华弹"
)

# 扫描 n-gram 锚点（略宽，用于 jieba OOV 预筛）
POLYPHONE_CHARS = _TTS_RISKY_CHARS

# Step/TTS 已能读对的常见搭配（可随录制反馈追加，非全量词库）
SKIP_COMPOUNDS = frozenset(
    {
        "调查",
        "调查处",
        "特殊调查处",
        "调整",
        "处理",
        "到处",
        "调动",
        "调节",
        "空调",
        "强调",
        "语调",
        "腔调",
        "调子",
        "调休",
        "调档",
        "调离",
        "调职",
        "调令",
        "调遣",
        "调兵",
        "调虎",
        "处决",
        "处罚",
        "处分",
        "处境",
        "处于",
        "处长",
        "处长",
        "到处",
        "长处",
        "短处",
        "难处",
        "住处",
        "用处",
        "办事处",
        "重要",
        "重复",
        "重新",
        "重来",
        "长期",
        "长大",
        "成长",
        "生长",
        "长大",
        "朝阳",
        "朝代",
        "朝着",
        "下降",
        "降低",
        "血压",
        "血液",
        "差别",
        "差异",
        "差不多",
        "发现",
        "发展",
        "头发",
        "理发",
        "和平",
        "和气",
        "还有",
        "还是",
        "得了",
        "觉得",
        "得到",
        "地方",
        "地点",
        "看着",
        "跟着",
        "干部",
        "干净",
        "更加",
        "曾经",
        "行为",
        "银行",
        "方便",
        "数量",
        "质量",
        "传递",
        "传说",
        "当然",
        "当时",
        "将来",
        "少数",
        "概率",
        "快乐",
        "音乐",
        "解决",
        "解释",
        "间隔",
        "记载",
        "感觉",
        "睡觉",
        "积累",
        "宁静",
        "参加",
        "收藏",
        "折断",
        "度过",
        "薄弱",
        "宁愿",
        "胜利",
        "华丽",
    }
)

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_ORG_SUFFIX_CHARS = frozenset("处局司部科组队阁殿宫署院库坊府庄寨帮")
_BAD_ORG_STEM_START = frozenset("是否在从到把被让给跟无这那但而也就还又")
_MIN_OOV_LEN = 2
_MAX_OOV_LEN = 8
_DEFAULT_MIN_FREQ_OOV = 2
_MAX_SCAN_RESULTS = 200


@dataclass(frozen=True)
class ScanCandidate:
    source_text: str
    hit_count: int
    context_sample: str
    source_kind: str  # character_name | oov_ngram


def contains_polyphone(text: str) -> bool:
    """词组中是否含 TTS 易读错的多音字（仅「处/行」等后缀不算）。"""
    src = (text or "").strip()
    if not src:
        return False
    risky = [ch for ch in src if ch in _TTS_RISKY_CHARS]
    if not risky:
        return False
    non_suffix = [ch for ch in risky if ch not in _ORG_SUFFIX_CHARS]
    return bool(non_suffix)


def is_pronunciation_candidate(
    text: str,
    *,
    hit_count: int,
    min_freq: int,
) -> bool:
    """扫描/展示共用：须含多音字且达到最低出现次数。"""
    src = (text or "").strip()
    if len(src) < _MIN_OOV_LEN:
        return False
    if hit_count < min_freq:
        return False
    return contains_polyphone(src)


def _character_names_in_db(conn) -> set[str]:
    return {
        (row.get("name") or "").strip()
        for row in list_characters(conn)
        if (row.get("name") or "").strip()
    }


def is_actor_or_proper_name(source_text: str, *, character_names: set[str] | None = None) -> bool:
    """演员表人名、纯专名不应注入 pronunciation_map（易触发模型念出声调数字）。"""
    src = (source_text or "").strip()
    if not src:
        return False
    if character_names and src in character_names:
        return True
    if len(src) <= 6 and not any(ch in src for ch in _ORG_SUFFIX_CHARS):
        if not contains_polyphone(src):
            return True
        risky = [ch for ch in src if ch in _TTS_RISKY_CHARS and ch not in _ORG_SUFFIX_CHARS]
        if len(risky) <= 1 and len(src) <= 4:
            return True
    return False


# StepAudio 官方 pronunciation_map.tone 示例（见 stepaudio-2.5-tts 文档）：
#   阿胶/e1胶   — 仅对多音字注声调数字，其余保持汉字
#   扁舟/偏舟   — 同音替代表达期望读音
#   嫉妒/ji2妒   — 混合：多音字用拼音+调号，邻字保持汉字
# 禁止整词空格分隔音节：郭长城/guo1 chang2 cheng2（2.5 会念出「长2」）
_STEP_TONE_OFFICIAL_EXAMPLES = (
    "阿胶/e1胶",
    "扁舟/偏舟",
    "嫉妒/ji2妒",
    "特调处/特diao4处",
)
# 整词全拼（多空格音节）视为非法
_INVALID_SPACED_PINYIN = re.compile(r"(?:[a-z]+\d\s+){1,}[a-z]+\d", re.I)
# 调+处/查 → 读 diào
_DIAO4_AFTER_CHARS = frozenset("处查研")


def is_valid_step_tone_rule(rule: str) -> bool:
    """是否符合 StepAudio pronunciation_map.tone 官方写法。"""
    text = (rule or "").strip()
    if "/" not in text:
        return False
    src, _, reading = text.partition("/")
    if not src.strip() or not reading.strip():
        return False
    if _INVALID_SPACED_PINYIN.search(reading):
        return False
    return True


def format_step_tone_rule(source_text: str, reading: str) -> str:
    """组装单条 tone 规则；reading 侧请遵循官方混合注音格式。"""
    src = (source_text or "").strip()
    rhs = (reading or "").strip()
    if not src or not rhs:
        return ""
    rule = f"{src}/{rhs}"
    return rule if is_valid_step_tone_rule(rule) else ""


def _preferred_tone3_for_char(ch: str, src: str, index: int) -> str:
    """按上下文挑选多音字读音（tone3：diao4、chang2 等）。"""
    options = pinyin(ch, style=Style.TONE3, heteronym=True, errors="ignore")
    if not options or not options[0]:
        return ""
    readings = options[0]
    if ch == "调":
        nxt = src[index + 1] if index + 1 < len(src) else ""
        if nxt in _DIAO4_AFTER_CHARS:
            for py in readings:
                if py.startswith("diao"):
                    return py
    if ch == "长" and index + 1 < len(src) and src[index + 1] == "城":
        for py in readings:
            if py.startswith("chang"):
                return py
    return readings[0]


def default_tone_rule(source_text: str, reading: str | None = None) -> str:
    """
    生成 StepAudio tone 规则（对齐官方 extra_body.pronunciation_map.tone）。

    自动建议格式：仅替换词内多音字为「拼音+调号」，其余字保持汉字，
    如 特调处 → 特调处/特diao4处，对应文档 阿胶/e1胶、嫉妒/ji2妒。
    """
    src = (source_text or "").strip()
    if not src:
        return ""
    if reading:
        return format_step_tone_rule(src, reading.strip())
    out: list[str] = []
    changed = False
    for i, ch in enumerate(src):
        if ch not in _TTS_RISKY_CHARS or ch in _ORG_SUFFIX_CHARS:
            out.append(ch)
            continue
        py = _preferred_tone3_for_char(ch, src, i)
        if py and py[-1].isdigit():
            out.append(py)
            changed = True
        else:
            out.append(ch)
    if not changed:
        return ""
    return format_step_tone_rule(src, "".join(out))


def sanitize_tone_rules_for_api(rules: list[str]) -> list[str]:
    """送入 TTS 前最后一道过滤，剔除非法 tone 字符串。"""
    out: list[str] = []
    for rule in rules:
        text = (rule or "").strip()
        if text and is_valid_step_tone_rule(text):
            out.append(text)
    return out


def is_known_jieba_word(phrase: str) -> bool:
    """整词可被 jieba 识别为单一词条时视为常见词，跳过。"""
    text = (phrase or "").strip()
    if not text:
        return True
    parts = jieba.lcut(text, HMM=False)
    return len(parts) == 1 and parts[0] == text


def dedupe_substring_terms(candidates: list[ScanCandidate]) -> list[ScanCandidate]:
    """
    最长匹配去重；同一簇内优先保留出现次数更高者。
    若「该归特调处」(1 次) 与「特调处」(2 次) 并存，保留后者。
    """
    if not candidates:
        return []
    ordered = sorted(
        candidates,
        key=lambda c: (-c.hit_count, -len(c.source_text), c.source_text),
    )
    kept: list[ScanCandidate] = []
    for cand in ordered:
        if any(
            cand.source_text in other.source_text and cand.source_text != other.source_text
            for other in kept
        ):
            continue
        kept = [
            other
            for other in kept
            if not (
                cand.source_text in other.source_text
                and cand.source_text != other.source_text
                and cand.hit_count >= other.hit_count
            )
        ]
        kept.append(cand)
    return kept


def _script_corpus(conn) -> str:
    rows = conn.execute("SELECT content FROM script_lines ORDER BY chapter_num, line_idx").fetchall()
    return "\n".join(str(r[0] or "") for r in rows)


def _count_phrase(corpus: str, phrase: str) -> int:
    if not phrase:
        return 0
    return corpus.count(phrase)


def _context_sample(corpus: str, phrase: str, *, width: int = 36) -> str:
    idx = corpus.find(phrase)
    if idx < 0:
        return ""
    start = max(0, idx - width)
    end = min(len(corpus), idx + len(phrase) + width)
    snippet = corpus[start:end].replace("\n", " ")
    return snippet.strip()


def _add_candidate(
    bag: dict[str, ScanCandidate],
    corpus: str,
    phrase: str,
    *,
    source_kind: str,
    min_freq: int,
) -> None:
    text = (phrase or "").strip()
    if len(text) < _MIN_OOV_LEN:
        return
    if text in SKIP_COMPOUNDS:
        return
    if is_known_jieba_word(text):
        return
    hit = _count_phrase(corpus, text)
    if not is_pronunciation_candidate(text, hit_count=hit, min_freq=min_freq):
        return
    prev = bag.get(text)
    ctx = _context_sample(corpus, text)
    if prev is None or hit > prev.hit_count:
        bag[text] = ScanCandidate(
            source_text=text,
            hit_count=hit,
            context_sample=ctx,
            source_kind=source_kind,
        )


def _valid_org_phrase(phrase: str) -> bool:
    text = (phrase or "").strip()
    if len(text) < 3 or text[-1] not in _ORG_SUFFIX_CHARS:
        return False
    stem = text[:-1]
    if stem and stem[0] in _BAD_ORG_STEM_START:
        return False
    return True


def _collect_org_like_phrases(corpus: str, *, min_freq: int = 1) -> Counter[str]:
    """
    以机构后缀字（处/局/司…）为锚点，向左最多取 4 字，得到规范专名候选。
    避免滑窗匹配出「事该归特调处」类脏数据。
    """
    found: set[str] = set()
    for i, ch in enumerate(corpus):
        if ch not in _ORG_SUFFIX_CHARS:
            continue
        for stem_len in range(1, 5):
            start = i - stem_len
            if start < 0:
                continue
            phrase = corpus[start : i + 1]
            if not _CJK_RUN_RE.fullmatch(phrase):
                continue
            if not _valid_org_phrase(phrase):
                continue
            found.add(phrase)
    counts: Counter[str] = Counter()
    for phrase in found:
        hit = corpus.count(phrase)
        if hit >= min_freq:
            counts[phrase] = hit
    return counts


def _collect_jieba_oov_tokens(corpus: str, min_freq: int) -> list[str]:
    counts: Counter[str] = Counter()
    for token in jieba.lcut(corpus):
        text = (token or "").strip()
        if not _CJK_RUN_RE.fullmatch(text or ""):
            continue
        if len(text) < 3 or len(text) > _MAX_OOV_LEN:
            continue
        counts[text] += 1
    return [p for p, c in counts.items() if c >= min_freq]


def scan_pronunciation_candidates(
    conn,
    *,
    min_freq_oov: int = _DEFAULT_MIN_FREQ_OOV,
    include_character_names: bool = False,
    max_results: int = _MAX_SCAN_RESULTS,
) -> list[ScanCandidate]:
    """本地扫描剧本与演员表，零 LLM token。"""
    corpus = _script_corpus(conn)
    if not corpus.strip():
        return []

    by_phrase: dict[str, ScanCandidate] = {}

    if include_character_names:
        for row in list_characters(conn):
            name = (row.get("name") or "").strip()
            _add_candidate(
                by_phrase,
                corpus,
                name,
                source_kind="character_name",
                min_freq=min_freq_oov,
            )

    for phrase, _hit in _collect_org_like_phrases(corpus, min_freq=min_freq_oov).items():
        _add_candidate(
            by_phrase,
            corpus,
            phrase,
            source_kind="org_like",
            min_freq=min_freq_oov,
        )

    merged = dedupe_substring_terms(list(by_phrase.values()))
    merged.sort(
        key=lambda c: (
            0 if c.source_kind in ("character_name", "org_like") else 1,
            -c.hit_count,
            -len(c.source_text),
            c.source_text,
        )
    )
    if max_results > 0:
        merged = merged[:max_results]
    return merged


def merge_scan_into_pending(
    conn,
    novel_fingerprint: str,
    candidates: list[ScanCandidate],
) -> int:
    """将扫描候选写入库（status=pending），不覆盖已 confirmed/ignored。"""
    fp = (novel_fingerprint or "").strip()
    if not fp:
        return 0
    existing = {
        r["source_text"]: r["status"]
        for r in list_pronunciation_rules(conn, fp)
    }
    added = 0
    for cand in candidates:
        if cand.source_text in existing:
            continue
        upsert_pronunciation_rule(
            conn,
            novel_fingerprint=fp,
            source_text=cand.source_text,
            tone_rule=default_tone_rule(cand.source_text),
            status=PRONUNCIATION_STATUS_PENDING,
            hit_count=cand.hit_count,
            context_sample=cand.context_sample,
        )
        added += 1
    return added


def purge_pending_rules_not_matching(
    conn,
    novel_fingerprint: str,
    *,
    min_freq: int,
) -> int:
    """删除待确认中：无多音字或未达最低出现次数的项（清理误扫）。"""
    from db import delete_pronunciation_rule

    fp = (novel_fingerprint or "").strip()
    if not fp:
        return 0
    removed = 0
    for row in list_pronunciation_rules(conn, fp, status=PRONUNCIATION_STATUS_PENDING):
        src = (row.get("source_text") or "").strip()
        hit = int(row.get("hit_count") or 0)
        if is_pronunciation_candidate(src, hit_count=hit, min_freq=min_freq):
            continue
        delete_pronunciation_rule(conn, int(row["id"]))
        removed += 1
    return removed


def filter_rules_for_display(
    rows: list[dict[str, Any]],
    *,
    min_freq: int,
    include_ignored: bool = False,
) -> list[dict[str, Any]]:
    """规则列表展示：已确认全显示；待确认按频次+多音字筛选。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        status = (row.get("status") or "").strip()
        src = (row.get("source_text") or "").strip()
        hit = int(row.get("hit_count") or 0)
        if status == PRONUNCIATION_STATUS_CONFIRMED:
            out.append(row)
            continue
        if status == PRONUNCIATION_STATUS_IGNORED:
            if include_ignored:
                out.append(row)
            continue
        if status == PRONUNCIATION_STATUS_PENDING:
            if is_pronunciation_candidate(src, hit_count=hit, min_freq=min_freq):
                out.append(row)
    return out


def filter_maximal_tone_rules(
    rules: list[tuple[str, str]],
) -> list[str]:
    """
    对 (source_text, tone_rule) 做最长匹配过滤。
    若短词是真子串 of 长词，丢弃短项。
    """
    if not rules:
        return []
    ordered = sorted(rules, key=lambda x: (-len(x[0]), x[0]))
    kept: list[tuple[str, str]] = []
    for src, tone in ordered:
        if not src or not tone:
            continue
        if any(src in longer and src != longer for longer, _ in kept):
            continue
        kept.append((src, tone))
    return [tone for _, tone in kept]


def tone_rules_for_spoken_text(
    confirmed_rules: list[dict[str, Any]],
    spoken: str,
    *,
    character_names: set[str] | None = None,
) -> list[str]:
    """按当前待读正文筛选并最长去重，供 pronunciation_map.tone 使用。"""
    text = spoken or ""
    if not text.strip():
        return []
    pairs: list[tuple[str, str]] = []
    for row in confirmed_rules:
        src = (row.get("source_text") or "").strip()
        tone = (row.get("tone_rule") or "").strip()
        if not src or not tone or src not in text:
            continue
        if is_actor_or_proper_name(src, character_names=character_names):
            continue
        if not is_valid_step_tone_rule(tone):
            continue
        pairs.append((src, tone))
    return sanitize_tone_rules_for_api(filter_maximal_tone_rules(pairs))


def load_confirmed_rules_for_api(conn, novel_fingerprint: str) -> list[dict[str, Any]]:
    fp = (novel_fingerprint or "").strip()
    if not fp:
        return []
    names = _character_names_in_db(conn)
    return [
        row
        for row in list_confirmed_pronunciation_rules(conn, fp)
        if not is_actor_or_proper_name(
            str(row.get("source_text") or ""), character_names=names
        )
        and is_valid_step_tone_rule(str(row.get("tone_rule") or ""))
    ]


def prune_substring_rules_on_confirm(
    conn,
    novel_fingerprint: str,
    confirmed_source: str,
) -> None:
    """确认较长词组后，自动忽略其真子串 pending/confirmed 规则。"""
    from db import delete_pronunciation_rule

    fp = (novel_fingerprint or "").strip()
    src = (confirmed_source or "").strip()
    if not fp or not src:
        return
    for row in list_pronunciation_rules(conn, fp):
        other = (row.get("source_text") or "").strip()
        if not other or other == src:
            continue
        if other in src and len(other) < len(src):
            delete_pronunciation_rule(conn, int(row["id"]))


def export_rules_json(rules: list[dict[str, Any]]) -> str:
    payload = [
        {
            "source_text": r.get("source_text"),
            "tone_rule": r.get("tone_rule"),
            "status": r.get("status"),
            "note": r.get("note", ""),
        }
        for r in rules
        if (r.get("status") or "") == PRONUNCIATION_STATUS_CONFIRMED
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def import_rules_json(conn, novel_fingerprint: str, raw: str) -> int:
    fp = (novel_fingerprint or "").strip()
    if not fp:
        return 0
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("JSON 须为数组")
    n = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        src = (item.get("source_text") or "").strip()
        if not src:
            continue
        tone = (item.get("tone_rule") or "").strip() or default_tone_rule(src)
        upsert_pronunciation_rule(
            conn,
            novel_fingerprint=fp,
            source_text=src,
            tone_rule=tone,
            status=PRONUNCIATION_STATUS_CONFIRMED,
            note=(item.get("note") or "").strip(),
        )
        prune_substring_rules_on_confirm(conn, fp, src)
        n += 1
    return n
