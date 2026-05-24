"""章首「第N章 + 章名」与正文首句拆分（剧本入库与库内修复共用）。"""

from __future__ import annotations

import re
from typing import Any

CHAPTER_MARKER_RE = re.compile(
    r"^第\s*([0-9一二三四五六七八九十百千万零〇两]+)\s*章"
)

# 正文常见起始（章名之后）
_BODY_START_RE = re.compile(
    r"^("
    r"[0-9○一二三四五六七八九十百千万零〇两]{2,8}"
    r"|刹那间|忽然|突然|于是|这时|此刻|那天|这天|后来|之前|之后|只见|只听"
    r"|他|她|它|这|那|当|随|正|已|却|便|又|再|还|就|也|不|没|有|在|到|从|被|把|让|给"
    r"|所以|因此|因为|虽然|但是|然而|不过|而且|并且|或者|如果|即使"
    r"|「|『|\u201c|\u201d"
    r")"
)

_HAS_SPEECH = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaffa-zA-Z0-9]"
)


def build_chapter_subtitles_map(novel_text: str) -> dict[int, str]:
    from pipeline import collect_all_chapter_marker_hits

    out: dict[int, str] = {}
    for hit in collect_all_chapter_marker_hits(novel_text or ""):
        num = int(hit["chapter_num"])
        if num in out:
            continue
        title = (hit.get("chapter_title") or "").strip()
        if title:
            out[num] = title
    return out


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


def try_split_chapter_opening_line(
    row: dict[str, Any],
    chapter_num: int,
    titles_map: dict[int, str],
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

    m = CHAPTER_MARKER_RE.match(content)
    marker = m.group(0).strip()
    rest = content[m.end() :].lstrip()
    if not rest:
        return None

    known = (titles_map.get(int(chapter_num)) or "").strip()
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
    titles_map: dict[int, str],
) -> list[dict[str, Any]]:
    if not lines:
        return lines
    split = try_split_chapter_opening_line(lines[0], chapter_num, titles_map)
    if not split:
        return lines
    title_c, body_c = split
    first = lines[0]
    title_row = {
        **first,
        "content": title_c,
        "emotion_instruction": "章标题",
        "is_dialogue": False,
    }
    body_row = {
        **first,
        "content": body_c,
        "emotion_instruction": (first.get("emotion_instruction") or "").strip()
        or "平缓叙述",
        "is_dialogue": False,
    }
    return [title_row, body_row, *lines[1:]]


def split_first_script_line_in_db(
    conn,
    chapter_num: int,
    titles_map: dict[int, str],
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

    split = try_split_chapter_opening_line(
        {
            "role": row["role"],
            "content": row["content"],
            "emotion_instruction": row["emotion_instruction"],
            "is_dialogue": bool(row["is_dialogue"]),
        },
        chapter_num,
        titles_map,
    )
    if not split:
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
        or "平缓叙述",
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
