"""敏感内容：按句/字符不断切片拆解；仍无法通过则记入待手动录入并跳过。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from .pipeline_log import PipelineLog

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?…])")

# 细分到不超过该句数或字数仍 451 / 覆盖率过低 → 提示用户手动补剧本并跳过
MANUAL_BLOCK_MAX_SENTENCES = int(os.environ.get("B2A_MANUAL_BLOCK_MAX_SENTENCES", "3"))
MANUAL_BLOCK_MAX_CHARS = int(os.environ.get("B2A_MANUAL_BLOCK_MAX_CHARS", "150"))
# 子段剧本字数 / 原文字数（去标点）低于此视为「输出被掐断」，继续二分
SEGMENT_COVERAGE_MIN = float(os.environ.get("B2A_SEGMENT_COVERAGE_MIN", "0.72"))


class CensorshipBlockedError(RuntimeError):
    """StepPlan 返回 451 / censorship_blocked，或子段覆盖率过低（等同审核掐断）。"""

    def __init__(self, message: str, *, status_code: int = 451) -> None:
        super().__init__(message)
        self.status_code = status_code


def is_censorship_blocked_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if isinstance(exc, CensorshipBlockedError):
        return True
    return "451" in msg or "censorship_blocked" in msg or (
        "blocked" in msg and "censor" in msg
    )


def split_sentence_units(text: str) -> list[str]:
    """按句号/问号/叹号等切分，保留标点在前一段末尾。"""
    if not text or not text.strip():
        return []
    parts = _SENTENCE_SPLIT_RE.split(text)
    return [p for p in parts if p and p.strip()]


def segment_parse_coverage(segment: str, parsed_lines: list[Any]) -> float:
    """子段剧本相对该段原文的覆盖率（去标点字数比）。"""
    from pipeline import text_without_punctuation

    target = (segment or "").strip()
    if not target:
        return 1.0
    combined = ""
    for item in parsed_lines:
        if isinstance(item, dict):
            combined += str(item.get("content") or item.get("text") or "")
    if not combined.strip():
        return 0.0
    src = text_without_punctuation(target)
    scr = text_without_punctuation(combined)
    if not src:
        return 1.0
    return len(scr) / len(src)


@dataclass
class BlockedTextSegment:
    """章内一段无法自动拆解的原文（相对 chapter.text 的偏移）。"""

    char_start: int
    char_end: int
    snippet: str
    reason: str = "censorship_blocked"


def _chapter_with_subtext(chapter: Any, subtext: str) -> Any:
    from pipeline import NovelChapter

    return NovelChapter(
        index=chapter.index,
        chapter_num=chapter.chapter_num,
        start=chapter.start,
        end=chapter.end,
        text=subtext,
        overlap_prefix=0,
        slice_mode="sub",
        preamble_chars=0,
    )


def _too_small_to_split(segment: str) -> bool:
    units = split_sentence_units(segment)
    if len(units) <= MANUAL_BLOCK_MAX_SENTENCES:
        return True
    if len(segment.strip()) <= MANUAL_BLOCK_MAX_CHARS:
        return True
    return False


def _bisect_segment(segment: str) -> tuple[str, str] | None:
    """按句号二分；仅一句时按字符中点二分。"""
    units = split_sentence_units(segment)
    if len(units) > 1:
        mid = max(1, len(units) // 2)
        left = "".join(units[:mid])
        right = "".join(units[mid:])
        if left.strip() and right.strip():
            return left, right
    text = segment.strip()
    if len(text) > MANUAL_BLOCK_MAX_CHARS:
        mid = max(1, len(text) // 2)
        return text[:mid], text[mid:]
    return None


def _record_manual_block(
    blocked: list[BlockedTextSegment],
    sink: PipelineLog,
    chapter_num: int,
    rel_start: int,
    rel_end: int,
    segment: str,
    exc: BaseException,
) -> None:
    snippet = segment.strip()
    sink.warn(
        f"第 {chapter_num} 章 · 以下原文已细分至约 {len(snippet)} 字仍无法自动生成剧本，"
        f"请在本页「待手动录入」中补充后点确认，流水线将跳过本段并继续：\n"
        f"{snippet[:500]}"
        + ("…" if len(snippet) > 500 else "")
    )
    blocked.append(
        BlockedTextSegment(
            char_start=rel_start,
            char_end=rel_end,
            snippet=snippet,
            reason=str(exc)[:200],
        )
    )


def _parse_segment_attempt(
    parser: Callable[..., dict[str, Any]],
    api_key: str,
    chapter: Any,
    segment: str,
    total_chapters: int,
    memory_json: str,
    *,
    incomplete_names: list[str] | None,
    sink: PipelineLog,
) -> tuple[list[Any], list[Any]]:
    sub = _chapter_with_subtext(chapter, segment)
    payload = parser(
        api_key,
        sub,
        total_chapters,
        memory_json,
        incomplete_names=incomplete_names,
        log=sink,
    )
    part_lines = payload.get("parsed_lines") or []
    if not part_lines:
        raise CensorshipBlockedError("模型返回空 parsed_lines")
    cov = segment_parse_coverage(segment, part_lines)
    if cov < SEGMENT_COVERAGE_MIN:
        raise CensorshipBlockedError(
            f"子段覆盖率 {cov:.1%} 低于 {SEGMENT_COVERAGE_MIN:.0%}（疑似输出被审核掐断）"
        )
    return part_lines, list(payload.get("characters_delta") or [])


def _attempt_range(
    parser: Callable[..., dict[str, Any]],
    api_key: str,
    chapter: Any,
    full_text: str,
    total_chapters: int,
    memory_json: str,
    *,
    incomplete_names: list[str] | None,
    sink: PipelineLog,
    merged_lines: list[Any],
    merged_delta: list[Any],
    blocked: list[BlockedTextSegment],
    rel_start: int,
    rel_end: int,
    depth: int,
) -> None:
    segment = full_text[rel_start:rel_end]
    if not segment.strip():
        return
    seg_len = len(segment)

    try:
        if depth == 0:
            sink.progress(
                f"第 {chapter.chapter_num} 章：整章请求（{seg_len} 字）…"
            )
        else:
            sink.progress(
                f"第 {chapter.chapter_num} 章 · 子段 "
                f"[{rel_start}:{rel_end}]（{seg_len} 字，切片 depth={depth}）…"
            )
        part_lines, part_delta = _parse_segment_attempt(
            parser,
            api_key,
            chapter,
            segment,
            total_chapters,
            memory_json,
            incomplete_names=incomplete_names,
            sink=sink,
        )
        merged_lines.extend(part_lines)
        merged_delta.extend(part_delta)
    except Exception as exc:
        if not is_censorship_blocked_error(exc):
            raise
        if _too_small_to_split(segment):
            _record_manual_block(
                blocked, sink, chapter.chapter_num, rel_start, rel_end, segment, exc
            )
            return
        parts = _bisect_segment(segment)
        if not parts:
            _record_manual_block(
                blocked, sink, chapter.chapter_num, rel_start, rel_end, segment, exc
            )
            return
        left_part, right_part = parts
        left_len = len(left_part)
        units_n = len(split_sentence_units(segment))
        sink.progress(
            f"第 {chapter.chapter_num} 章 · 子段审核/覆盖率拦截，"
            f"继续切片（约 {units_n} 句 → 左右两段）…"
        )
        _attempt_range(
            parser,
            api_key,
            chapter,
            full_text,
            total_chapters,
            memory_json,
            incomplete_names=incomplete_names,
            sink=sink,
            merged_lines=merged_lines,
            merged_delta=merged_delta,
            blocked=blocked,
            rel_start=rel_start,
            rel_end=rel_start + left_len,
            depth=depth + 1,
        )
        _attempt_range(
            parser,
            api_key,
            chapter,
            full_text,
            total_chapters,
            memory_json,
            incomplete_names=incomplete_names,
            sink=sink,
            merged_lines=merged_lines,
            merged_delta=merged_delta,
            blocked=blocked,
            rel_start=rel_start + left_len,
            rel_end=rel_end,
            depth=depth + 1,
        )


def llm_parse_chapter_with_sensitive_split(
    api_key: str,
    chapter: Any,
    total_chapters: int,
    memory_json: str,
    *,
    incomplete_names: list[str] | None = None,
    log: PipelineLog | None = None,
    parse_fn: Callable[..., dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[BlockedTextSegment]]:
    """
    先整章请求；遇 451 或子段覆盖率过低则不断切片，能过则过；
    细分到约 {MANUAL_BLOCK_MAX_SENTENCES} 句 / {MANUAL_BLOCK_MAX_CHARS} 字仍不过 → 记入 blocked 并跳过该段。
    """
    from pipeline import llm_parse_chapter_payload

    sink = log or PipelineLog()
    parser = parse_fn or llm_parse_chapter_payload
    full_text = chapter.text or ""
    merged_lines: list[Any] = []
    merged_delta: list[Any] = []
    blocked: list[BlockedTextSegment] = []

    _attempt_range(
        parser,
        api_key,
        chapter,
        full_text,
        total_chapters,
        memory_json,
        incomplete_names=incomplete_names,
        sink=sink,
        merged_lines=merged_lines,
        merged_delta=merged_delta,
        blocked=blocked,
        rel_start=0,
        rel_end=len(full_text),
        depth=0,
    )

    return {
        "parsed_lines": merged_lines,
        "characters_delta": merged_delta,
    }, blocked


def sensitive_fill_chapter_gaps(
    api_key: str,
    chapter: Any,
    total_chapters: int,
    memory_json: str,
    existing_payload: dict[str, Any],
    *,
    incomplete_names: list[str] | None = None,
    log: PipelineLog | None = None,
    coverage_min: float | None = None,
) -> tuple[dict[str, Any], list[BlockedTextSegment]]:
    """
    章内已有部分剧本但覆盖率仍不足时，对估算缺口做敏感切片拆解（不重复整章请求）。
    """
    from pipeline import chapter_parse_coverage

    sink = log or PipelineLog()
    parsed = list(existing_payload.get("parsed_lines") or [])
    cov_min = coverage_min if coverage_min is not None else float(
        os.environ.get("B2A_CHAPTER_COVERAGE_MIN", "0.98")
    )
    coverage = chapter_parse_coverage(chapter, parsed)
    if coverage >= cov_min:
        return existing_payload, []

    full_text = chapter.text or ""
    if not full_text.strip():
        return existing_payload, []

    # 按已覆盖比例估算缺口起点，略回退以免漏句
    est_start = max(0, int(len(full_text) * coverage) - 300)
    gap_text = full_text[est_start:]
    if len(gap_text.strip()) < 20:
        return existing_payload, []

    sink.progress(
        f"第 {chapter.chapter_num} 章覆盖率 {coverage:.1%} 不足，"
        f"对后段约 {len(gap_text)} 字继续切片拆解…"
    )
    gap_chapter = _chapter_with_subtext(chapter, gap_text)
    gap_payload, blocked_raw = llm_parse_chapter_with_sensitive_split(
        api_key,
        gap_chapter,
        total_chapters,
        memory_json,
        incomplete_names=incomplete_names,
        log=sink,
    )
    blocked = [
        BlockedTextSegment(
            char_start=seg.char_start + est_start,
            char_end=seg.char_end + est_start,
            snippet=seg.snippet,
            reason=seg.reason,
        )
        for seg in blocked_raw
    ]
    extra_lines = gap_payload.get("parsed_lines") or []
    if extra_lines:
        parsed.extend(extra_lines)
    merged_delta = list(existing_payload.get("characters_delta") or [])
    merged_delta.extend(gap_payload.get("characters_delta") or [])
    return {
        "parsed_lines": parsed,
        "characters_delta": merged_delta,
    }, blocked
