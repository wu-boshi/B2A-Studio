"""剧本 + 演员表离线 CSV 导出/导入（统一「记录类型」列区分剧本行与演员行）。"""

from __future__ import annotations

import csv
import io
from typing import Any

from db import (
    CHARACTER_FIELDS,
    IMPORTANCE_EXTRA,
    IMPORTANCE_MAIN,
    IMPORTANCE_PENDING,
    IMPORTANCE_STOCK,
    NARRATOR_NAME,
    ensure_database,
    ensure_narrator_character,
    get_connection,
    upsert_character,
)

RECORD_SCRIPT = "剧本"
RECORD_CAST = "演员"

# 记录类型 + 剧本列 + 演员列（与 app 导出中文表头一致）
OFFLINE_CSV_HEADERS: dict[str, str] = {
    "record_type": "记录类型",
    "chapter_num": "章",
    "line_idx": "行号",
    "role": "角色",
    "emotion_instruction": "语气指令",
    "content": "正文",
    "is_dialogue": "是否对白",
    "line_voice_id": "行音色ID",
    "gender": "性别",
    "age": "年龄",
    "personality": "人设",
    "quote_1": "代表台词1",
    "quote_2": "代表台词2",
    "quote_1_instruction": "台词1情绪",
    "quote_2_instruction": "台词2情绪",
    "voice_id": "音色ID",
    "importance_level": "重要度",
}

_OFFLINE_FIELD_ORDER = list(OFFLINE_CSV_HEADERS.keys())

_COL_RECORD_TYPE = frozenset({"记录类型", "类型", "record_type", "row_type"})
_COL_CHAPTER = frozenset({"章", "chapter_num", "章节", "章节号"})
_COL_LINE = frozenset({"行号", "line_idx", "行", "line"})
_COL_ROLE = frozenset({"角色", "role", "人物", "姓名", "name"})
_COL_EMOTION = frozenset({"语气指令", "emotion_instruction", "情绪", "情绪指令"})
_COL_CONTENT = frozenset({"正文", "content", "台词", "内容"})
_COL_DIALOGUE = frozenset({"是否对白", "is_dialogue", "对白"})
_COL_LINE_VOICE = frozenset({"行音色ID", "line_voice_id", "剧本音色"})
_COL_GENDER = frozenset({"性别", "gender"})
_COL_AGE = frozenset({"年龄", "age"})
_COL_PERSONALITY = frozenset({"人设", "personality"})
_COL_Q1 = frozenset({"代表台词1", "quote_1"})
_COL_Q2 = frozenset({"代表台词2", "quote_2"})
_COL_Q1I = frozenset({"台词1情绪", "quote_1_instruction"})
_COL_Q2I = frozenset({"台词2情绪", "quote_2_instruction"})
_COL_VOICE = frozenset({"音色ID", "voice_id", "音色"})
_COL_IMPORTANCE = frozenset({"重要度", "importance_level", "等级"})

_CAST_RECORD_ALIASES = frozenset(
    {RECORD_CAST, "演员表", "cast", "character", "characters", "角色表"}
)
_SCRIPT_RECORD_ALIASES = frozenset(
    {RECORD_SCRIPT, "剧本行", "script", "script_line", "台词行"}
)

_VALID_IMPORTANCE = frozenset(
    {IMPORTANCE_PENDING, IMPORTANCE_MAIN, IMPORTANCE_EXTRA, IMPORTANCE_STOCK}
)


class ScriptCsvImportError(ValueError):
    """CSV 格式或内容无法导入。"""


def _pick(row: dict[str, str], keys: frozenset[str]) -> str:
    for key in keys:
        if key in row and row[key] is not None:
            return str(row[key]).strip()
    return ""


def _normalize_header_row(fieldnames: list[str] | None) -> dict[str, str]:
    """中文/英文表头 -> 内部字段名。"""
    rev = {v: k for k, v in OFFLINE_CSV_HEADERS.items()}
    out: dict[str, str] = {}
    for raw in fieldnames or []:
        key = str(raw or "").strip()
        if not key:
            continue
        out[key] = rev.get(key, key)
    return out


def _parse_dialogue_flag(raw: str) -> int:
    text = (raw or "").strip().lower()
    if text in ("是", "1", "true", "yes", "y", "对白", "dialogue"):
        return 1
    if text in ("否", "0", "false", "no", "n", "旁白", "narration"):
        return 0
    return 1 if text else 0


def _parse_record_type(raw: str) -> str:
    text = (raw or "").strip().lower()
    if not text:
        return RECORD_SCRIPT
    if text in {a.lower() for a in _CAST_RECORD_ALIASES}:
        return RECORD_CAST
    if text in {a.lower() for a in _SCRIPT_RECORD_ALIASES}:
        return RECORD_SCRIPT
    if text in ("演员", "cast"):
        return RECORD_CAST
    if text in ("剧本", "script"):
        return RECORD_SCRIPT
    return RECORD_SCRIPT


def _parse_importance(raw: str) -> str:
    text = (raw or "").strip().lower()
    if text in _VALID_IMPORTANCE:
        return text
    mapping = {
        "主": IMPORTANCE_MAIN,
        "主要": IMPORTANCE_MAIN,
        "配角": IMPORTANCE_EXTRA,
        "龙套": IMPORTANCE_STOCK,
        "待定": IMPORTANCE_PENDING,
        "pending": IMPORTANCE_PENDING,
        "main": IMPORTANCE_MAIN,
        "extra": IMPORTANCE_EXTRA,
        "stock": IMPORTANCE_STOCK,
    }
    return mapping.get(text, IMPORTANCE_PENDING)


def _row_to_internal(
    row: dict[str, Any], header_map: dict[str, str]
) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for k, v in row.items():
        internal = header_map.get(str(k).strip(), str(k).strip())
        normalized[internal] = "" if v is None else str(v).strip()
    return normalized


def _normalize_script_row(normalized: dict[str, str]) -> dict[str, Any] | None:
    content = _pick(normalized, _COL_CONTENT)
    if not content:
        return None
    ch_raw = _pick(normalized, _COL_CHAPTER)
    line_raw = _pick(normalized, _COL_LINE)
    if not ch_raw or not line_raw:
        raise ScriptCsvImportError("剧本行须包含「章」与「行号」。")
    try:
        chapter_num = int(float(ch_raw))
        line_idx = int(float(line_raw))
    except ValueError as exc:
        raise ScriptCsvImportError(
            f"章/行号须为整数：章={ch_raw!r} 行号={line_raw!r}"
        ) from exc
    if chapter_num < 1 or line_idx < 1:
        raise ScriptCsvImportError("章号与行号须 ≥ 1。")
    role = _pick(normalized, _COL_ROLE) or "旁白"
    return {
        "chapter_num": chapter_num,
        "line_idx": line_idx,
        "role": role,
        "emotion_instruction": _pick(normalized, _COL_EMOTION),
        "content": content,
        "is_dialogue": _parse_dialogue_flag(_pick(normalized, _COL_DIALOGUE)),
        "voice_id": _pick(normalized, _COL_LINE_VOICE),
    }


def _normalize_cast_row(normalized: dict[str, str]) -> dict[str, Any] | None:
    name = _pick(normalized, _COL_ROLE)
    if not name:
        return None
    return {
        "name": name,
        "gender": _pick(normalized, _COL_GENDER),
        "age": _pick(normalized, _COL_AGE),
        "personality": _pick(normalized, _COL_PERSONALITY),
        "quote_1": _pick(normalized, _COL_Q1),
        "quote_2": _pick(normalized, _COL_Q2),
        "quote_1_instruction": _pick(normalized, _COL_Q1I),
        "quote_2_instruction": _pick(normalized, _COL_Q2I),
        "voice_id": _pick(normalized, _COL_VOICE),
        "importance_level": _parse_importance(_pick(normalized, _COL_IMPORTANCE)),
    }


def parse_offline_csv_text(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    解析离线 CSV。返回 (剧本行, 演员行)。
    无「记录类型」列时，整表按旧版纯剧本 CSV 解析（不含演员行）。
    """
    raw = (text or "").strip()
    if not raw:
        raise ScriptCsvImportError("CSV 文件为空。")
    if raw.startswith("\ufeff"):
        raw = raw.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        raise ScriptCsvImportError("CSV 缺少表头行。")
    header_map = _normalize_header_row(list(reader.fieldnames))
    has_record_type = any(
        str(h).strip() in _COL_RECORD_TYPE for h in (reader.fieldnames or [])
    )

    script_rows: list[dict[str, Any]] = []
    cast_rows: list[dict[str, Any]] = []

    for i, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue
        normalized = _row_to_internal(row, header_map)
        try:
            if has_record_type:
                rec = _parse_record_type(
                    normalized.get("record_type")
                    or _pick(normalized, _COL_RECORD_TYPE)
                )
                if rec == RECORD_CAST:
                    parsed = _normalize_cast_row(normalized)
                    if parsed:
                        cast_rows.append(parsed)
                    continue
                parsed = _normalize_script_row(normalized)
            else:
                parsed = _normalize_script_row(normalized)
        except ScriptCsvImportError:
            raise
        except Exception as exc:
            raise ScriptCsvImportError(f"第 {i} 行解析失败：{exc}") from exc
        if parsed:
            script_rows.append(parsed)

    if not script_rows and not cast_rows:
        raise ScriptCsvImportError("未找到有效剧本行或演员行。")
    return script_rows, cast_rows


def parse_script_csv_text(text: str) -> list[dict[str, Any]]:
    """兼容旧接口：仅返回剧本行。"""
    script_rows, _ = parse_offline_csv_text(text)
    if not script_rows:
        raise ScriptCsvImportError("未找到有效剧本行（正文列为空）。")
    return script_rows


def _format_dialogue(val: Any) -> str:
    if val in (1, True, "1", "true", "True"):
        return "是"
    if val in (0, False, "0", "false", "False"):
        return "否"
    return str(val or "")


def _encode_offline_csv(rows: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=[OFFLINE_CSV_HEADERS[k] for k in _OFFLINE_FIELD_ORDER],
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in rows:
        out: dict[str, str] = {}
        for key in _OFFLINE_FIELD_ORDER:
            label = OFFLINE_CSV_HEADERS[key]
            val = row.get(key, "")
            if key == "is_dialogue":
                val = _format_dialogue(val)
            out[label] = "" if val is None else str(val)
        writer.writerow(out)
    return buf.getvalue().encode("utf-8-sig")


def _fetch_script_lines_for_export(
    conn, *, chapter_num: int | None = None
) -> list[dict[str, Any]]:
    sql = """
        SELECT chapter_num, line_idx, role, emotion_instruction,
               content, is_dialogue, voice_id
        FROM script_lines
    """
    params: list[Any] = []
    if chapter_num is not None:
        sql += " WHERE chapter_num = ?"
        params.append(chapter_num)
    sql += " ORDER BY chapter_num, line_idx"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _fetch_cast_for_export(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT name, gender, age, personality,
               quote_1, quote_2, quote_1_instruction, quote_2_instruction,
               voice_id, importance_level
        FROM characters
        ORDER BY CASE WHEN name = ? THEN 0 ELSE 1 END, name COLLATE NOCASE
        """,
        (NARRATOR_NAME,),
    ).fetchall()
    return [dict(r) for r in rows]


def build_offline_csv_rows(
    script_rows: list[dict[str, Any]],
    cast_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """合并为带「记录类型」的导出行。"""
    out: list[dict[str, Any]] = []
    for s in script_rows:
        out.append(
            {
                "record_type": RECORD_SCRIPT,
                "chapter_num": s.get("chapter_num", ""),
                "line_idx": s.get("line_idx", ""),
                "role": s.get("role", ""),
                "emotion_instruction": s.get("emotion_instruction", ""),
                "content": s.get("content", ""),
                "is_dialogue": s.get("is_dialogue", 0),
                "line_voice_id": s.get("voice_id", ""),
            }
        )
    for c in cast_rows:
        out.append(
            {
                "record_type": RECORD_CAST,
                "role": c.get("name", ""),
                "gender": c.get("gender", ""),
                "age": c.get("age", ""),
                "personality": c.get("personality", ""),
                "quote_1": c.get("quote_1", ""),
                "quote_2": c.get("quote_2", ""),
                "quote_1_instruction": c.get("quote_1_instruction", ""),
                "quote_2_instruction": c.get("quote_2_instruction", ""),
                "voice_id": c.get("voice_id", ""),
                "importance_level": c.get("importance_level", ""),
            }
        )
    return out


def export_offline_csv_bytes(
    conn, *, chapter_num: int | None = None
) -> bytes:
    """导出剧本行（可选单章）+ 全书演员表。"""
    script_rows = _fetch_script_lines_for_export(conn, chapter_num=chapter_num)
    cast_rows = _fetch_cast_for_export(conn)
    combined = build_offline_csv_rows(script_rows, cast_rows)
    return _encode_offline_csv(combined)


def _upsert_cast_with_importance(conn, data: dict[str, Any]) -> None:
    name = (data.get("name") or "").strip()
    if not name or name == NARRATOR_NAME:
        return
    payload = {field: str(data.get(field) or "").strip() for field in CHARACTER_FIELDS}
    payload["name"] = name
    upsert_character(conn, payload)
    imp = (data.get("importance_level") or "").strip()
    if imp in _VALID_IMPORTANCE and imp != IMPORTANCE_PENDING:
        conn.execute(
            "UPDATE characters SET importance_level = ? WHERE name = ?",
            (imp, name),
        )


def _apply_narrator_from_csv(conn, data: dict[str, Any]) -> None:
    ensure_narrator_character(conn)
    sets: list[str] = []
    params: list[Any] = []
    for col, key in (
        ("gender", "gender"),
        ("age", "age"),
        ("personality", "personality"),
        ("quote_1", "quote_1"),
        ("quote_2", "quote_2"),
        ("quote_1_instruction", "quote_1_instruction"),
        ("quote_2_instruction", "quote_2_instruction"),
        ("voice_id", "voice_id"),
    ):
        val = str(data.get(key) or "").strip()
        if val:
            sets.append(f"{col} = ?")
            params.append(val)
    if sets:
        params.append(NARRATOR_NAME)
        conn.execute(
            f"UPDATE characters SET {', '.join(sets)} WHERE name = ?",
            params,
        )


def import_cast_rows(conn, cast_rows: list[dict[str, Any]]) -> int:
    narrator_row: dict[str, Any] | None = None
    count = 0
    for row in cast_rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        if name == NARRATOR_NAME:
            narrator_row = row
            continue
        _upsert_cast_with_importance(conn, row)
        count += 1
    ensure_narrator_character(conn)
    if narrator_row:
        _apply_narrator_from_csv(conn, narrator_row)
        count += 1
    return count


def import_script_rows(
    conn,
    rows: list[dict[str, Any]],
    *,
    replace_existing: bool = True,
    auto_create_cast_from_roles: bool = True,
) -> dict[str, int]:
    if replace_existing:
        conn.execute("DELETE FROM script_lines")
        if auto_create_cast_from_roles:
            conn.execute("DELETE FROM characters")

    roles: set[str] = set()
    inserted = 0
    for row in rows:
        role = str(row["role"] or "旁白").strip() or "旁白"
        roles.add(role)
        voice_id = str(row.get("voice_id") or "").strip()
        conn.execute(
            """
            INSERT INTO script_lines (
                chapter_num, line_idx, role, voice_id,
                emotion_instruction, content, is_dialogue,
                recording_status, recording_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '', '')
            """,
            (
                int(row["chapter_num"]),
                int(row["line_idx"]),
                role,
                voice_id,
                str(row.get("emotion_instruction") or "").strip(),
                str(row["content"] or "").strip(),
                int(row["is_dialogue"]),
            ),
        )
        inserted += 1

    if auto_create_cast_from_roles:
        existing = {
            str(r[0])
            for r in conn.execute("SELECT name FROM characters").fetchall()
        }
        for role in sorted(roles):
            if role in (NARRATOR_NAME,) or role in existing:
                continue
            upsert_character(
                conn,
                {
                    "name": role,
                    "gender": "",
                    "age": "",
                    "personality": "",
                    "quote_1": "",
                    "quote_2": "",
                    "quote_1_instruction": "",
                    "quote_2_instruction": "",
                    "voice_id": "",
                },
            )
        ensure_narrator_character(conn)

    chapters = len({int(r["chapter_num"]) for r in rows}) if rows else 0
    conn.commit()
    return {
        "lines": inserted,
        "chapters": chapters,
        "roles": len(roles),
    }


def import_offline_bundle(
    conn,
    script_rows: list[dict[str, Any]],
    cast_rows: list[dict[str, Any]],
    *,
    replace_existing: bool = True,
) -> dict[str, int]:
    if replace_existing:
        conn.execute("DELETE FROM script_lines")
        conn.execute("DELETE FROM characters")

    cast_imported = 0
    if cast_rows:
        cast_imported = import_cast_rows(conn, cast_rows)
    elif replace_existing:
        ensure_narrator_character(conn)

    if script_rows:
        stats = import_script_rows(
            conn,
            script_rows,
            replace_existing=False,
            auto_create_cast_from_roles=not bool(cast_rows),
        )
    else:
        conn.commit()
        stats = {"lines": 0, "chapters": 0, "roles": 0}

    stats["cast"] = cast_imported
    if cast_rows:
        stats["roles"] = len(
            {str(r["name"]) for r in cast_rows if r.get("name")}
            | {str(r["role"]) for r in script_rows if r.get("role")}
        )
    return stats


def import_script_csv_bytes(
    data: bytes,
    *,
    replace_existing: bool = True,
    novel_fingerprint: str | None = None,
) -> dict[str, int]:
    from db import clear_checkpoints

    text = data.decode("utf-8-sig", errors="replace")
    script_rows, cast_rows = parse_offline_csv_text(text)
    ensure_database()
    with get_connection() as conn:
        # 离线导入不依赖 pipeline 断点；清空全部检查点，避免误报指纹不一致
        clear_checkpoints(conn, None)
        if novel_fingerprint:
            conn.execute(
                "DELETE FROM blocked_script_segments WHERE novel_fingerprint = ?",
                (novel_fingerprint,),
            )
        else:
            conn.execute("DELETE FROM blocked_script_segments")
        conn.commit()
        if cast_rows or script_rows:
            return import_offline_bundle(
                conn,
                script_rows,
                cast_rows,
                replace_existing=replace_existing,
            )
        raise ScriptCsvImportError("CSV 无有效数据。")


def import_script_csv_file(
    path: str | Any,
    *,
    replace_existing: bool = True,
) -> dict[str, int]:
    from pathlib import Path

    data = Path(path).read_bytes()
    return import_script_csv_bytes(data, replace_existing=replace_existing)
