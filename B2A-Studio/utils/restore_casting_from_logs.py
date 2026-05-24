#!/usr/bin/env python3
"""
从 recording.log + 已恢复行的 actual_voice_id 推断角色→音色，写回 characters 与 script_lines。
仅恢复「日志/音频中有证据」的映射；其余角色不猜测，避免与未录行试镜冲突。
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from db import NARRATOR_NAME, ensure_narrator_character, get_connection, init_schema

from utils.b2a_paths import B2A_ROOT, REPO_ROOT

ROOT = REPO_ROOT
LOG_PATH = ROOT / "B2A-Studio" / "logs" / "recording.log"
VOICES_JSON = B2A_ROOT / "data" / "step_tts2_system_voices.json"
EXPORT_PATH = ROOT / "B2A-Studio" / "logs" / "casting_restored_from_logs.csv"

# 旁白固定
NARRATOR_VOICE = "ruyananshi"

_RE_MIC = re.compile(r"🎙️\s*第\s*\d+\s*章\s*·\s*行\s*\d+/\d+\s*·\s*(.+)$")
_RE_VOICE = re.compile(r"voice=([^\s（|]+)")


def _load_voice_labels() -> dict[str, str]:
    if not VOICES_JSON.is_file():
        return {}
    try:
        rows = json.loads(VOICES_JSON.read_text(encoding="utf-8"))
        return {
            str(r["voice_id"]): str(r.get("display_name") or r["voice_id"])
            for r in rows
            if r.get("voice_id")
        }
    except (OSError, json.JSONDecodeError):
        return {}


def infer_from_recording_log() -> dict[str, str]:
    role_counts: dict[str, Counter] = defaultdict(Counter)
    current: str | None = None
    if not LOG_PATH.is_file():
        return {}
    for line in LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _RE_MIC.search(line)
        if m:
            current = m.group(1).split("·")[0].strip()
            continue
        if not current or "voice=" not in line:
            continue
        if not any(
            k in line for k in ("已缓存", "Step 整句成功", "行 OK", "切片拼接完成")
        ):
            continue
        mv = _RE_VOICE.search(line)
        if mv:
            role_counts[current][mv.group(1)] += 1

    out: dict[str, str] = {NARRATOR_NAME: NARRATOR_VOICE}
    for role, ctr in role_counts.items():
        if not ctr:
            continue
        vid, n = ctr.most_common(1)[0]
        total = sum(ctr.values())
        # 至少 3 次且占比 >60% 才采纳，减少串台
        if n >= 3 and n / total >= 0.6:
            out[role] = vid
        elif n >= 10 and n / total >= 0.85:
            out[role] = vid
    return out


def infer_from_db_actual() -> dict[str, str]:
    out: dict[str, str] = {}
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT role, actual_voice_id, COUNT(*) c
            FROM script_lines
            WHERE TRIM(COALESCE(actual_voice_id, '')) != ''
              AND actual_voice_id NOT LIKE 'silence:%'
              AND actual_voice_id NOT LIKE 'edge:%'
            GROUP BY role, actual_voice_id
            ORDER BY role, c DESC
            """
        ).fetchall()
    by_role: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_role[str(r["role"])][str(r["actual_voice_id"])] += int(r["c"])
    for role, ctr in by_role.items():
        vid, _ = ctr.most_common(1)[0]
        out[role] = vid
    return out


def merge_mappings(*maps: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {NARRATOR_NAME: NARRATOR_VOICE}
    for m in maps:
        merged.update(m)
    return merged


def apply_to_db(mapping: dict[str, str]) -> tuple[int, int]:
    labels = _load_voice_labels()
    char_n = 0
    line_n = 0
    with get_connection() as conn:
        init_schema(conn)
        ensure_narrator_character(conn)
        conn.execute(
            "UPDATE characters SET voice_id = ? WHERE name = ?",
            (NARRATOR_VOICE, NARRATOR_NAME),
        )
        char_n += 1

        for role, vid in sorted(mapping.items()):
            if role == NARRATOR_NAME:
                continue
            cur = conn.execute(
                "UPDATE characters SET voice_id = ? WHERE name = ?",
                (vid, role),
            )
            if cur.rowcount:
                char_n += cur.rowcount
            cur2 = conn.execute(
                "UPDATE script_lines SET voice_id = ? WHERE role = ?",
                (vid, role),
            )
            line_n += cur2.rowcount

        conn.commit()

        # 导出对照表
        rows = conn.execute(
            """
            SELECT name, gender, age, voice_id, importance_level
            FROM characters
            WHERE name != ?
            ORDER BY name COLLATE NOCASE
            """,
            (NARRATOR_NAME,),
        ).fetchall()
    EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines_out = ["角色,性别,年龄,音色ID,音色名,来源,重要度"]
    for r in rows:
        name = str(r["name"])
        vid = str(r["voice_id"] or "")
        src = "日志+已录音频" if name in mapping else "未恢复(需试镜或龙套池)"
        lines_out.append(
            f"{name},{r['gender'] or ''},{r['age'] or ''},{vid},"
            f"{labels.get(vid, vid)},{src},{r['importance_level'] or ''}"
        )
    EXPORT_PATH.write_text("\n".join(lines_out) + "\n", encoding="utf-8-sig")
    return char_n, line_n


def main() -> None:
    from_log = infer_from_recording_log()
    from_db = infer_from_db_actual()
    mapping = merge_mappings(from_db, from_log)
    print(f"推断到 {len(mapping)} 个角色音色映射：")
    labels = _load_voice_labels()
    for role in sorted(mapping.keys()):
        vid = mapping[role]
        print(f"  {role}: {vid} ({labels.get(vid, vid)})")
    char_n, line_n = apply_to_db(mapping)
    print(f"\n已写回 characters: {char_n} 行, script_lines.voice_id: {line_n} 行")
    print(f"对照表: {EXPORT_PATH}")
    print(
        "\n说明：仅恢复在 recording.log 或已录 MP3 中有证据的角色。"
        "其余配角若曾绑定试镜音色，旧库已无法找回；可在试镜厅用龙套池按性别年龄批量套用。"
    )


if __name__ == "__main__":
    main()
