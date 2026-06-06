"""配音演员表离线导出 / 按角色名导入（新书指纹变更后复用音色）。"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db import (
    IMPORTANCE_EXTRA,
    IMPORTANCE_MAIN,
    IMPORTANCE_STOCK,
    NARRATOR_NAME,
    ROLLING_RANK_TOP_N,
    get_connection,
    init_schema,
)
from .extra_stock import STOCK_EXTRA_SLOTS, fetch_stock_extra_characters
from .step_audio import bundled_system_voices

from utils.b2a_paths import APP_DIR, B2A_ROOT
BACKUP_DIR = APP_DIR / "backups"


def _voice_label_map() -> dict[str, str]:
    return {
        str(v.voice_id): str(v.display_name or v.voice_id)
        for v in bundled_system_voices()
        if v.voice_id
    }


def export_casting_backup(
    novel_title: str = "",
    *,
    dest_dir: Path | None = None,
) -> tuple[Path, Path]:
    """
    导出旁白 + 主演 + 龙套池 + 已绑定 extra 配角音色到 JSON 与 CSV。
    返回 (json_path, csv_path)。
    """
    labels = _voice_label_map()
    out_dir = dest_dir or BACKUP_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_title = (novel_title or "未命名").strip() or "未命名"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base = out_dir / f"casting_{safe_title}_{stamp}"

    with get_connection() as conn:
        init_schema(conn)
        rows = conn.execute(
            """
            SELECT
                name, gender, age, personality,
                quote_1, quote_2,
                quote_1_instruction, quote_2_instruction,
                voice_id, importance_level
            FROM characters
            ORDER BY
                CASE importance_level
                    WHEN 'stock' THEN 0
                    WHEN 'main' THEN 1
                    WHEN 'extra' THEN 2
                    ELSE 3
                END,
                name COLLATE NOCASE
            """
        ).fetchall()
        stock = fetch_stock_extra_characters(conn)

    characters: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        vid = str(item.get("voice_id") or "").strip()
        item["voice_label"] = labels.get(vid, vid)
        characters.append(item)

    payload: dict[str, Any] = {
        "format": "b2a-casting-backup-v1",
        "novel_title": safe_title,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "cast_policy": {
            "narrator": 1,
            "main_top_n": ROLLING_RANK_TOP_N,
            "stock_slots": len(STOCK_EXTRA_SLOTS),
        },
        "narrator": next(
            (c for c in characters if c.get("name") == NARRATOR_NAME),
            None,
        ),
        "main_cast": [
            c for c in characters if c.get("importance_level") == IMPORTANCE_MAIN
        ],
        "stock_pool": [dict(s) for s in stock],
        "extra_with_voice": [
            c
            for c in characters
            if c.get("importance_level") == IMPORTANCE_EXTRA
            and str(c.get("voice_id") or "").strip()
        ],
        "voice_bindings": [
            {
                "name": c.get("name"),
                "voice_id": str(c.get("voice_id") or "").strip(),
                "importance_level": c.get("importance_level"),
            }
            for c in characters
            if str(c.get("voice_id") or "").strip()
        ],
        "all_characters": characters,
    }

    json_path = base.with_suffix(".json")
    csv_path = base.with_suffix(".csv")
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "name",
                "importance_level",
                "gender",
                "age",
                "voice_id",
                "voice_label",
                "personality",
                "quote_1",
                "quote_2",
            ],
        )
        writer.writeheader()
        for c in characters:
            vid = str(c.get("voice_id") or "").strip()
            writer.writerow(
                {
                    "name": c.get("name"),
                    "importance_level": c.get("importance_level"),
                    "gender": c.get("gender"),
                    "age": c.get("age"),
                    "voice_id": vid,
                    "voice_label": labels.get(vid, vid),
                    "personality": c.get("personality"),
                    "quote_1": c.get("quote_1"),
                    "quote_2": c.get("quote_2"),
                }
            )

    latest_json = out_dir / f"casting_{safe_title}_latest.json"
    latest_csv = out_dir / f"casting_{safe_title}_latest.csv"
    latest_json.write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    latest_csv.write_text(csv_path.read_text(encoding="utf-8-sig"), encoding="utf-8-sig")
    return json_path, csv_path


def import_casting_backup(
    backup_path: Path,
    *,
    apply_stock: bool = True,
    apply_main: bool = True,
    apply_narrator: bool = True,
    apply_extra: bool = False,
    apply_all_voice_bindings: bool = True,
) -> dict[str, int]:
    """
    按角色名写回 voice_id（需先完成剧本拆解、角色已入库）。
    默认恢复旁白 / 主演 / 龙套池，并写回备份中全部已绑定音色（voice_bindings）。
    """
    from db import bind_character_voice

    data = json.loads(backup_path.read_text(encoding="utf-8"))
    if data.get("format") != "b2a-casting-backup-v1":
        raise ValueError("不支持的备份格式")

    updated = 0
    lines = 0
    with get_connection() as conn:
        init_schema(conn)

        def _apply_row(name: str, voice_id: str) -> None:
            nonlocal updated, lines
            name = (name or "").strip()
            vid = (voice_id or "").strip()
            if not name or not vid:
                return
            exists = conn.execute(
                "SELECT 1 FROM characters WHERE name = ?", (name,)
            ).fetchone()
            if not exists:
                return
            result = bind_character_voice(conn, name, vid)
            updated += 1
            lines += int(result.get("script_lines_updated") or 0)

        if apply_narrator:
            narr = data.get("narrator") or {}
            _apply_row(str(narr.get("name") or NARRATOR_NAME), str(narr.get("voice_id") or ""))

        if apply_main:
            for row in data.get("main_cast") or []:
                if str(row.get("name") or "").strip() == NARRATOR_NAME:
                    continue
                _apply_row(str(row.get("name")), str(row.get("voice_id") or ""))

        if apply_stock:
            for row in data.get("stock_pool") or []:
                _apply_row(str(row.get("name")), str(row.get("voice_id") or ""))

        if apply_extra:
            for row in data.get("extra_with_voice") or []:
                _apply_row(str(row.get("name")), str(row.get("voice_id") or ""))

        if apply_all_voice_bindings:
            seen: set[str] = set()
            for row in data.get("voice_bindings") or []:
                name = str(row.get("name") or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                _apply_row(name, str(row.get("voice_id") or ""))

        conn.commit()

    return {"characters_updated": updated, "script_lines_updated": lines}


def list_casting_backups(novel_title: str = "") -> list[Path]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    title = (novel_title or "").strip()
    paths = sorted(BACKUP_DIR.glob("casting_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if title:
        paths = [p for p in paths if title in p.name]
    return paths
