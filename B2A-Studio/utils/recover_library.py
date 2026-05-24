#!/usr/bin/env python3
"""
从 剧本 CSV + 行级 MP3 缓存 + recording.log 恢复剧本库与录制进度。
在 ensure_database 误删库或界面空白后运行一次。
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .audiobook_assembly import audio_duration_seconds, gap_seconds_for_line
from .audiobook_paths import line_cache_audio_path
from db import (
    DB_PATH,
    RECORDING_STATUS_OK,
    get_connection,
    init_schema,
    update_script_line_audio_tracking,
)

from utils.b2a_paths import B2A_ROOT

LOG_PATH = B2A_ROOT / "logs" / "recording.log"

_RE_CH_LINE = re.compile(r"🎙️\s*第\s*(\d+)\s*章\s*·\s*行\s*(\d+)/")
_RE_OK = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ✓ 行 (\d+) 已缓存.*?voice=([^\s（]+)"
)


@dataclass
class CachedHit:
    ts: datetime
    chapter: int
    line_idx: int
    voice: str


def _parse_log_hits(log_path: Path) -> list[CachedHit]:
    hits: list[CachedHit] = []
    if not log_path.is_file():
        return hits
    current_ch: int | None = None
    for raw in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m_ch = _RE_CH_LINE.search(raw)
        if m_ch:
            current_ch = int(m_ch.group(1))
            continue
        m_ok = _RE_OK.match(raw)
        if m_ok and current_ch is not None:
            ts = datetime.strptime(m_ok.group(1), "%Y-%m-%d %H:%M:%S")
            hits.append(
                CachedHit(
                    ts=ts,
                    chapter=current_ch,
                    line_idx=int(m_ok.group(2)),
                    voice=m_ok.group(3),
                )
            )
    return hits


def _import_csv(conn: sqlite3.Connection, csv_path: Path) -> int:
    from utils.script_csv_io import import_script_rows, parse_script_csv_text

    text = csv_path.read_text(encoding="utf-8-sig", errors="replace")
    rows = parse_script_csv_text(text)
    stats = import_script_rows(conn, rows, replace_existing=True)
    return int(stats["lines"])


def _line_id_map(conn: sqlite3.Connection) -> dict[tuple[int, int], int]:
    rows = conn.execute(
        "SELECT id, chapter_num, line_idx FROM script_lines"
    ).fetchall()
    return {(int(r["chapter_num"]), int(r["line_idx"])): int(r["id"]) for r in rows}


def _find_mp3_by_mtime(
    chapter_dir: Path,
    target_ts: datetime,
    used: set[Path],
) -> Path | None:
    if not chapter_dir.is_dir():
        return None
    best: Path | None = None
    best_delta = 999999.0
    target = target_ts.timestamp()
    for mp3 in chapter_dir.glob("line_*.mp3"):
        if mp3 in used:
            continue
        delta = abs(mp3.stat().st_mtime - target)
        if delta < best_delta and delta <= 120:
            best_delta = delta
            best = mp3
    return best


def _restore_caches(
    conn: sqlite3.Connection,
    novel_name: str,
    hits: list[CachedHit],
    cache_root: Path,
    *,
    app_dir: Path,
) -> tuple[int, int]:
    id_map = _line_id_map(conn)
    restored = 0
    missing = 0
    used_files: set[Path] = set()

    for hit in hits:
        key = (hit.chapter, hit.line_idx)
        line_id = id_map.get(key)
        if line_id is None:
            missing += 1
            continue
        ch_dir = cache_root / ".cache" / f"chapter_{hit.chapter:04d}"
        src = _find_mp3_by_mtime(ch_dir, hit.ts, used_files)
        if src is None:
            missing += 1
            continue
        dest = line_cache_audio_path(
            novel_name, hit.chapter, line_id, base_dir=app_dir
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.is_file() or dest.stat().st_size == 0:
            shutil.copy2(src, dest)
        used_files.add(src)
        raw = dest.read_bytes()
        duration = audio_duration_seconds(raw)
        row = conn.execute(
            "SELECT is_dialogue FROM script_lines WHERE id = ?",
            (line_id,),
        ).fetchone()
        is_dialogue = bool(row["is_dialogue"]) if row else False
        gap = gap_seconds_for_line(is_dialogue)
        update_script_line_audio_tracking(
            conn,
            line_id,
            actual_voice_id=hit.voice,
            audio_duration=duration,
            gap_duration=gap,
            start_time_offset=0.0,
            end_time_offset=duration,
        )
        restored += 1

    conn.commit()
    return restored, missing


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="从剧本 CSV + 行级 MP3 缓存 + recording.log 恢复剧本库与录制进度"
    )
    parser.add_argument(
        "--csv",
        type=Path,
        required=True,
        help="剧本 CSV 路径（如 剧本_全书.csv）",
    )
    parser.add_argument(
        "--novel",
        required=True,
        help="小说显示名（与 [小说名]_有声书 目录一致）",
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=None,
        help="行级 MP3 缓存根目录（默认 B2A-Studio/[小说名]_有声书）",
    )
    args = parser.parse_args()

    csv_path = args.csv.expanduser().resolve()
    novel_name = args.novel.strip()
    cache_root = args.cache_root
    if cache_root is None:
        cache_root = B2A_ROOT / f"{novel_name}_有声书"
    else:
        cache_root = cache_root.expanduser().resolve()

    print(f"数据库: {DB_PATH}")
    print(f"CSV: {csv_path}")
    print(f"缓存目录: {cache_root}")

    if DB_PATH.exists():
        bak = DB_PATH.with_suffix(".db.before-recover")
        shutil.copy2(DB_PATH, bak)
        print(f"已备份当前空库 -> {bak.name}")

    with get_connection() as conn:
        init_schema(conn)
        n_lines = _import_csv(conn, csv_path)
        print(f"已从 CSV 导入剧本行: {n_lines}")

        hits = _parse_log_hits(LOG_PATH)
        print(f"从 recording.log 解析到成功录制记录: {len(hits)}")

        restored, missing = _restore_caches(
            conn,
            novel_name,
            hits,
            cache_root,
            app_dir=B2A_ROOT,
        )
        ok_n = conn.execute(
            "SELECT COUNT(*) FROM script_lines WHERE recording_status = ?",
            (RECORDING_STATUS_OK,),
        ).fetchone()[0]
        print(f"已恢复行级缓存并写回库: {restored}，未匹配: {missing}")
        print(f"库内 recording_status=ok 行数: {ok_n}")
        print("角色音色需在试镜中重新绑定；剧本正文已恢复。")


if __name__ == "__main__":
    main()
