"""章首「第N章 + 章名」与正文首句拆分（剧本入库与库内修复共用）。

方案 A：以原文章首行结构为准，区分「仅章号 / 有副标题 / 章号后接正文」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

CHAPTER_MARKER_RE = re.compile(
    r"^第\s*([0-9一二三四五六七八九十百千万零〇两]+)\s*章"
)

# 正文常见起始（章名之后）
_BODY_START_RE = re.compile(
    r"^("
    r"[0-9○一二三四五六七八九十百千万零〇两]{2,8}"
    r"|刹那间|忽然|突然|于是|这时|此刻|那天|这天|后来|之前|之后|只见|只听"
    r"|我|你|他|她|它|这|那|当|随|正|已|却|便|又|再|还|就|也|不|没|有|在|到|从|被|把|让|给"
    r"|所以|因此|因为|虽然|但是|然而|不过|而且|并且|或者|如果|即使"
    r"|「|『|\u201c|\u201d"
    r")"
)

_HAS_SPEECH = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaffa-zA-Z0-9]"
)

ChapterOpeningKind = Literal["marker_only", "title", "inline_body"]

# 同一行章号后、可视为副标题的最大字数（不含「第N章」）
MAX_SUBTITLE_SPEECH_CHARS = 15


@dataclass(frozen=True)
class ChapterOpeningInfo:
    kind: ChapterOpeningKind
    marker: str = ""
    subtitle: str = ""
    title_line: str = ""

    @property
    def has_standalone_title(self) -> bool:
        return self.kind in ("marker_only", "title")


def extract_tail_after_marker(novel_text: str, marker_end: int) -> str:
    """「第N章」标记后、同一行内的剩余文本（不含换行后正文）。"""
    line_end = novel_text.find("\n", marker_end)
    if line_end < 0:
        line_end = len(novel_text)
    tail = novel_text[marker_end:line_end].strip()
    tail = re.sub(r"^[\s:：\-—·\.．、\[\]【】]+", "", tail)
    tail = re.sub(r"[\s:：\-—·\.．、]+$", "", tail)
    if not tail or CHAPTER_MARKER_RE.match(tail):
        return ""
    return tail


def classify_chapter_opening_tail(tail: str) -> tuple[ChapterOpeningKind, str]:
    """
    根据章号行内剩余文本判断章首类型。

    - marker_only：章号后无字（tail 为空）
    - title：短副标题（如「暗涌」）
    - inline_body：章号后与正文同在一行（如「我……」）
    """
    tail = (tail or "").strip()
    if not tail:
        return "marker_only", ""

    speech_len = _speech_len(tail)
    if speech_len > MAX_SUBTITLE_SPEECH_CHARS:
        return "inline_body", ""

    if _BODY_START_RE.match(tail):
        return "inline_body", ""

    if re.search(r"[。！？…；;]", tail):
        return "inline_body", ""

    if "，" in tail and speech_len > 8:
        return "inline_body", ""

    return "title", tail


def build_chapter_opening_info(
    marker_label: str,
    tail: str,
) -> ChapterOpeningInfo:
    marker = (marker_label or "").strip()
    kind, subtitle = classify_chapter_opening_tail(tail)
    if kind == "marker_only":
        title_line = marker
    elif kind == "title":
        title_line = f"{marker} {subtitle}".strip()
    else:
        title_line = ""
    return ChapterOpeningInfo(
        kind=kind,
        marker=marker,
        subtitle=subtitle,
        title_line=title_line,
    )


def build_chapter_openings_map(novel_text: str) -> dict[int, ChapterOpeningInfo]:
    from pipeline import collect_all_chapter_marker_hits

    out: dict[int, ChapterOpeningInfo] = {}
    for hit in collect_all_chapter_marker_hits(novel_text or ""):
        num = int(hit["chapter_num"])
        if num in out:
            continue
        marker_end = int(hit.get("marker_end") or 0)
        tail = extract_tail_after_marker(novel_text, marker_end)
        out[num] = build_chapter_opening_info(hit.get("label") or "", tail)
    return out


def build_chapter_subtitles_map(novel_text: str) -> dict[int, str]:
    """兼容旧接口：仅返回 kind=title 的副标题。"""
    return {
        num: info.subtitle
        for num, info in build_chapter_openings_map(novel_text).items()
        if info.kind == "title" and info.subtitle
    }


def _speech_len(text: str) -> int:
    return len(_HAS_SPEECH.findall(text or ""))


def find_body_start_index(rest: str) -> int:
    """在「章」标记后的剩余文本中，找章名结束、正文开始的位置。"""
    rest = (rest or "").strip()
    if len(rest) < 4:
        return 0
    for i in range(2, min(len(rest), 45)):
        m = _BODY_START_RE.match(rest[i:])
        if m and m.group(0):
            return i
    num_m = re.search(r"[0-9一二三四五六七八九十百千万零〇两]{2,}", rest)
    if num_m and num_m.start() >= 2:
        return num_m.start()
    return 0


def _strip_chapter_title_tag(row: dict[str, Any]) -> dict[str, Any]:
    emotion = (row.get("emotion_instruction") or "").strip()
    if emotion != "章标题" and not any(
        k in emotion for k in ("章名", "章节标题", "章节信息")
    ):
        return row
    fallback = emotion if emotion and emotion != "章标题" else "平缓叙述"
    return {**row, "emotion_instruction": fallback}


def _make_title_row(base: dict[str, Any], title_content: str) -> dict[str, Any]:
    return {
        **base,
        "content": title_content,
        "emotion_instruction": "章标题",
        "is_dialogue": False,
    }


def _make_body_row(base: dict[str, Any], body_content: str) -> dict[str, Any]:
    emotion = (base.get("emotion_instruction") or "").strip()
    if emotion == "章标题" or any(
        k in emotion for k in ("章名", "章节标题", "章节信息")
    ):
        emotion = "平缓叙述"
    return {
        **base,
        "content": body_content,
        "emotion_instruction": emotion or "平缓叙述",
        "is_dialogue": False,
    }


def try_split_chapter_opening_line(
    row: dict[str, Any],
    chapter_num: int,
    openings_map: dict[int, ChapterOpeningInfo] | None = None,
    *,
    titles_map: dict[int, str] | None = None,
) -> tuple[str, str] | None:
    """
    若首行旁白把章标题与正文首句拼在一起，拆为 (章标题行, 正文行)。
    无法可靠拆分时返回 None。
    """
    if (row.get("role") or "").strip() != "旁白":
        return None
    if row.get("is_dialogue"):
        return None
    content = (row.get("content") or "").strip()
    if not content or not CHAPTER_MARKER_RE.match(content):
        return None

    opening = (openings_map or {}).get(int(chapter_num))
    if opening and opening.kind == "inline_body":
        return None

    m = CHAPTER_MARKER_RE.match(content)
    marker = m.group(0).strip()
    rest = content[m.end() :].lstrip()

    if opening and opening.kind == "marker_only":
        if not rest:
            return None
        return marker, rest

    if opening and opening.kind == "title":
        known = opening.subtitle
        if known and rest.startswith(known):
            body = rest[len(known) :].lstrip()
            body = re.sub(r"^[\s:：\-—·\.．、]+", "", body)
            if _speech_len(body) < 3:
                return None
            return opening.title_line or f"{marker} {known}".strip(), body
        if not rest:
            return None
        idx = find_body_start_index(rest)
        if idx >= 2:
            title_part = rest[:idx].rstrip()
            body = rest[idx:].lstrip()
            if known and title_part != known:
                return None
            if _speech_len(body) >= 3 and _speech_len(title_part) >= 1:
                title_line = opening.title_line or f"{marker} {title_part}".strip()
                return title_line, body
        return None

    if not rest:
        return None

    legacy_titles = titles_map or {}
    known = (legacy_titles.get(int(chapter_num)) or "").strip()
    if known and rest.startswith(known):
        body = rest[len(known) :].lstrip()
        body = re.sub(r"^[\s:：\-—·\.．、]+", "", body)
        if _speech_len(body) < 3:
            return None
        title_line = f"{marker} {known}".strip()
        return title_line, body

    idx = find_body_start_index(rest)
    if idx < 2:
        return None
    title_part = rest[:idx].rstrip()
    body = rest[idx:].lstrip()
    if _speech_len(body) < 3 or _speech_len(title_part) < 1:
        return None
    title_line = f"{marker} {title_part}".strip()
    return title_line, body


def split_merged_chapter_opening_rows(
    lines: list[dict[str, Any]],
    chapter_num: int,
    openings_map: dict[int, ChapterOpeningInfo] | None = None,
    *,
    titles_map: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    if not lines:
        return lines

    opening = (openings_map or {}).get(int(chapter_num))
    first = lines[0]
    content = (first.get("content") or "").strip()

    if opening and opening.kind == "inline_body":
        return [_strip_chapter_title_tag(first), *lines[1:]]

    if opening and opening.has_standalone_title:
        split = try_split_chapter_opening_line(
            first,
            chapter_num,
            openings_map,
            titles_map=titles_map,
        )
        if split:
            title_c, body_c = split
            return [
                _make_title_row(first, title_c),
                _make_body_row(first, body_c),
                *lines[1:],
            ]

        expected = (opening.title_line or opening.marker or "").strip()
        if expected and content == expected:
            return [_make_title_row(first, content), *lines[1:]]

        if opening.kind == "marker_only" and CHAPTER_MARKER_RE.match(content):
            rest = content[CHAPTER_MARKER_RE.match(content).end() :].strip()
            if not rest:
                return [_make_title_row(first, content), *lines[1:]]

    legacy = titles_map if titles_map is not None else {}
    if not opening and legacy:
        split = try_split_chapter_opening_line(
            first, chapter_num, None, titles_map=legacy
        )
        if split:
            title_c, body_c = split
            return [
                _make_title_row(first, title_c),
                _make_body_row(first, body_c),
                *lines[1:],
            ]

    return lines


def split_first_script_line_in_db(
    conn,
    chapter_num: int,
    openings_map: dict[int, ChapterOpeningInfo] | None = None,
    *,
    titles_map: dict[int, str] | None = None,
) -> tuple[bool, list[int]]:
    """
    在库内拆分章首合并行：更新第 1 行标题、插入第 2 行正文。
    返回 (是否发生拆分, 需重录的 line_id 列表)。
    """
    from db import insert_script_line_manual

    row = conn.execute(
        """
        SELECT id, line_idx, role, content, emotion_instruction, is_dialogue, voice_id
        FROM script_lines
        WHERE chapter_num = ?
        ORDER BY line_idx
        LIMIT 1
        """,
        (int(chapter_num),),
    ).fetchone()
    if not row:
        return False, []

    opening = (openings_map or {}).get(int(chapter_num))
    row_dict = {
        "role": row["role"],
        "content": row["content"],
        "emotion_instruction": row["emotion_instruction"],
        "is_dialogue": bool(row["is_dialogue"]),
    }

    if opening and opening.kind == "inline_body":
        if (row_dict.get("emotion_instruction") or "").strip() != "章标题":
            return False, []
        conn.execute(
            """
            UPDATE script_lines SET
                emotion_instruction = '平缓叙述',
                recording_status = '',
                recording_error = '',
                actual_voice_id = '',
                audio_duration = NULL,
                gap_duration = NULL,
                start_time_offset = NULL,
                end_time_offset = NULL
            WHERE id = ?
            """,
            (int(row["id"]),),
        )
        return True, [int(row["id"])]

    split = try_split_chapter_opening_line(
        row_dict,
        chapter_num,
        openings_map,
        titles_map=titles_map,
    )
    if not split:
        expected = ""
        if opening and opening.has_standalone_title:
            expected = (opening.title_line or "").strip()
        content = (row_dict.get("content") or "").strip()
        if expected and content == expected and (
            row_dict.get("emotion_instruction") or ""
        ).strip() != "章标题":
            conn.execute(
                """
                UPDATE script_lines SET
                    emotion_instruction = '章标题',
                    recording_status = '',
                    recording_error = '',
                    actual_voice_id = '',
                    audio_duration = NULL,
                    gap_duration = NULL,
                    start_time_offset = NULL,
                    end_time_offset = NULL
                WHERE id = ?
                """,
                (int(row["id"]),),
            )
            return True, [int(row["id"])]
        return False, []

    title_c, body_c = split
    if title_c == (row["content"] or "").strip():
        return False, []

    line1_id = int(row["id"])
    conn.execute(
        """
        UPDATE script_lines SET
            content = ?,
            emotion_instruction = '章标题',
            is_dialogue = 0,
            recording_status = '',
            recording_error = '',
            actual_voice_id = '',
            audio_duration = NULL,
            gap_duration = NULL,
            start_time_offset = NULL,
            end_time_offset = NULL
        WHERE id = ?
        """,
        (title_c, line1_id),
    )
    new_idx = insert_script_line_manual(
        conn,
        chapter_num=int(chapter_num),
        after_line_idx=1,
        role=str(row["role"] or "旁白"),
        content=body_c,
        is_dialogue=False,
        emotion_instruction=(row["emotion_instruction"] or "").strip()
        if (row["emotion_instruction"] or "").strip() != "章标题"
        else "平缓叙述",
        voice_id=str(row["voice_id"] or ""),
    )
    new_row = conn.execute(
        """
        SELECT id FROM script_lines
        WHERE chapter_num = ? AND line_idx = ?
        """,
        (int(chapter_num), int(new_idx)),
    ).fetchone()
    rerun_ids = [line1_id]
    if new_row:
        rerun_ids.append(int(new_row["id"]))
    return True, rerun_ids
