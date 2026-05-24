"""Parse LLM script output: B2A block format (lossless) and tolerant JSON salvage."""

from __future__ import annotations

import json
import re
from typing import Any

BLOCK_BEGIN = "###B2A###"
BLOCK_END = "###END###"

_LINE_FIELD_ORDER = ("role", "emotion_instruction", "content", "is_dialogue", "voice_id")
_CHAR_FIELD_ORDER = (
    "name",
    "gender",
    "age",
    "personality",
    "quote_1",
    "quote_1_instruction",
    "quote_2",
    "quote_2_instruction",
)


def _unescape_json_string(value: str) -> str:
    """仅处理 JSON 标准转义，不改动未转义原文。"""
    return (
        value.replace(r"\\", "\\")
        .replace(r"\"", '"')
        .replace(r"\n", "\n")
        .replace(r"\r", "\r")
        .replace(r"\t", "\t")
    )


def _field_end_pattern(next_field: str) -> re.Pattern[str]:
    return re.compile(
        rf'"\s*,\s*(?:\n\s*)?"{re.escape(next_field)}"\s*:',
        re.MULTILINE,
    )


def _extract_quoted_field(
    block: str,
    field: str,
    next_fields: tuple[str, ...],
) -> str:
    """
    从 JSON 对象片段中提取带引号字段；content 内可有未转义 ASCII 引号。
    通过「下一个已知字段」定位结束位置，避免 json.loads 失败。
    """
    m = re.search(rf'"{re.escape(field)}"\s*:\s*"', block)
    if not m:
        return ""
    start = m.end()
    end = len(block)
    for nf in next_fields:
        pm = _field_end_pattern(nf).search(block, start)
        if pm:
            end = min(end, pm.start())
    return _unescape_json_string(block[start:end])


def _parse_script_line_json_block(block: str) -> dict[str, Any] | None:
    block = block.strip()
    if not block.startswith("{"):
        return None
    role = _extract_quoted_field(block, "role", ("emotion_instruction", "content"))
    emotion = _extract_quoted_field(
        block, "emotion_instruction", ("content", "is_dialogue", "voice_id")
    )
    content = _extract_quoted_field(
        block, "content", ("is_dialogue", "voice_id", "role")
    )
    if not content and not role:
        return None
    is_d = False
    dm = re.search(r'"is_dialogue"\s*:\s*(true|false)', block, re.I)
    if dm:
        is_d = dm.group(1).lower() == "true"
    voice = _extract_quoted_field(block, "voice_id", ("role", "emotion_instruction"))
    return {
        "role": role.strip() or "旁白",
        "emotion_instruction": emotion.strip(),
        "content": content,
        "is_dialogue": is_d,
        "voice_id": voice.strip(),
    }


def _parse_character_json_block(block: str) -> dict[str, Any] | None:
    block = block.strip()
    if not block.startswith("{"):
        return None
    name = _extract_quoted_field(block, "name", _CHAR_FIELD_ORDER[1:])
    if not name.strip():
        return None
    row: dict[str, Any] = {"name": name.strip()}
    for field in _CHAR_FIELD_ORDER[1:]:
        following = tuple(f for f in _CHAR_FIELD_ORDER if f != field)
        row[field] = _extract_quoted_field(block, field, following).strip()
    return row


def _split_json_array_object_blocks(text: str, key: str) -> list[str]:
    """按对象起始 { 切分数组元素（不依赖整段 json.loads）。"""
    key_idx = text.find(f'"{key}"')
    if key_idx < 0:
        return []
    arr_start = text.find("[", key_idx)
    if arr_start < 0:
        return []
    starts = [arr_start + m.start() + 1 for m in re.finditer(r"\n\s*\{", text[arr_start:])]
    blocks: list[str] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else text.find("\n  ]", start)
        if end < 0:
            end = len(text)
        chunk = text[start:end].strip().rstrip(",")
        if chunk.startswith("{"):
            blocks.append(chunk)
    return blocks


def salvage_script_json_text(text: str) -> dict[str, Any] | None:
    """字段边界法解析残缺 JSON，不丢弃含未转义引号的 content。"""
    line_blocks = _split_json_array_object_blocks(text, "parsed_lines")
    lines: list[dict[str, Any]] = []
    for block in line_blocks:
        row = _parse_script_line_json_block(block)
        if row and (row.get("content") or "").strip():
            lines.append(row)

    char_blocks = _split_json_array_object_blocks(text, "characters_delta")
    chars: list[dict[str, Any]] = []
    for block in char_blocks:
        row = _parse_character_json_block(block)
        if row:
            chars.append(row)

    if not lines and not chars:
        return None
    return {"parsed_lines": lines, "characters_delta": chars}


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "是")


def _parse_block_section_body(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    content_key = None
    content_lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped == ">>>":
            if content_key:
                fields[content_key] = "\n".join(content_lines)
                content_key = None
                content_lines = []
            continue
        if content_key is not None:
            content_lines.append(line)
            continue
        if "<<<" in line:
            key, _, _ = line.partition("=")
            content_key = (key.strip() or "content").replace("<<<", "").strip()
            if content_key.endswith("<<<"):
                content_key = content_key[:-3].strip()
            rest = line.split("<<<", 1)[-1].strip()
            if rest and rest != "<<<":
                content_lines.append(rest)
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip()
    return fields


def parse_b2a_block_format(text: str) -> dict[str, Any] | None:
    begin = text.find(BLOCK_BEGIN)
    if begin < 0:
        return None
    end = text.find(BLOCK_END, begin)
    body = text[begin + len(BLOCK_BEGIN) : end if end >= 0 else None].strip()

    lines: list[dict[str, Any]] = []
    chars: list[dict[str, Any]] = []

    for kind, chunk in re.findall(
        r"\[(character|line)\]([\s\S]*?)\[/\1\]",
        body,
        re.IGNORECASE,
    ):
        fields = _parse_block_section_body(chunk.strip())
        if kind.lower() == "line":
            content = fields.get("content", "")
            if not content.strip():
                continue
            lines.append(
                {
                    "role": fields.get("role", "旁白").strip() or "旁白",
                    "emotion_instruction": fields.get("emotion_instruction", "").strip(),
                    "content": content,
                    "is_dialogue": _parse_bool(fields.get("is_dialogue", "false")),
                    "voice_id": fields.get("voice_id", "").strip(),
                }
            )
        else:
            name = fields.get("name", "").strip()
            if not name:
                continue
            chars.append(
                {
                    "name": name,
                    "gender": fields.get("gender", "").strip(),
                    "age": fields.get("age", "").strip(),
                    "personality": fields.get("personality", "").strip(),
                    "quote_1": fields.get("quote_1", "").strip(),
                    "quote_2": fields.get("quote_2", "").strip(),
                    "quote_1_instruction": fields.get(
                        "quote_1_instruction", ""
                    ).strip(),
                    "quote_2_instruction": fields.get(
                        "quote_2_instruction", ""
                    ).strip(),
                }
            )

    if not lines and not chars:
        return None
    return {"parsed_lines": lines, "characters_delta": chars}


def try_json_repair_load(text: str) -> dict[str, Any] | None:
    try:
        import json_repair  # type: ignore
    except ImportError:
        return None
    try:
        obj = json_repair.loads(text)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def parse_script_output(text: str) -> tuple[dict[str, Any] | None, str]:
    """
    解析模型输出。返回 (payload, method_label)。
    method: block | json | json_repair | salvage | none
    """
    raw = (text or "").strip()
    if not raw:
        return None, "none"

    block = parse_b2a_block_format(raw)
    if block is not None:
        return block, "block"

    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj, "json"
    except json.JSONDecodeError:
        pass

    repaired = try_json_repair_load(raw)
    if repaired is not None:
        return repaired, "json_repair"

    salvaged = salvage_script_json_text(raw)
    if salvaged is not None:
        return salvaged, "salvage"

    return None, "none"
