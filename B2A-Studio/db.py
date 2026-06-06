"""SQLite persistence for B2A-Studio characters and script_lines."""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

_DB_INITIALIZED = False

APP_DIR = Path(__file__).resolve().parent
# 项目目录在 macOS 桌面/iCloud 上时 SQLite 易 disk I/O；库放在用户本地目录（非 iCloud）
LEGACY_DB_PATH = APP_DIR / "b2a_studio.db"
_DB_DIR_CANDIDATES = (
    Path.home() / "Library" / "Application Support" / "B2A-Studio",
    Path.home() / ".b2a_studio",
)


def _resolve_db_dir() -> Path:
    last_err: OSError | None = None
    for candidate in _DB_DIR_CANDIDATES:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError as exc:
            last_err = exc
    raise RuntimeError(
        "无法创建 B2A-Studio 数据库目录（已尝试 Application Support 与 ~/.b2a_studio）"
    ) from last_err


DB_DIR = _resolve_db_dir()
DB_PATH = DB_DIR / "b2a_studio.db"

CHARACTER_FIELDS = (
    "name",
    "gender",
    "age",
    "personality",
    "quote_1",
    "quote_2",
    "quote_1_instruction",
    "quote_2_instruction",
    "voice_id",
)

IMPORTANCE_PENDING = "pending"
IMPORTANCE_MAIN = "main"
IMPORTANCE_EXTRA = "extra"
IMPORTANCE_STOCK = "stock"
ROLLING_RANK_TOP_N = 12
NARRATOR_NAME = "旁白"
PERSONALITY_MAX_CHARS = 300

_NARRATOR_DEFAULT_PERSONALITY = (
    "全书叙述者与场景描写，承担章节衔接、环境氛围与心理外化。"
)
_NARRATOR_QUOTE_INSTRUCTIONS = (
    "沉稳、略带画面感的叙述",
    "情绪随情节起伏的描写",
)


def _is_disk_io_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "disk i/o" in msg or "disk i/o error" in msg or "i/o error" in msg


def _is_database_locked(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def _ensure_db_dir() -> None:
    global DB_DIR, DB_PATH
    DB_DIR = _resolve_db_dir()
    DB_PATH = DB_DIR / "b2a_studio.db"


def _db_has_user_data(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'script_lines'
            """
        ).fetchone()
        if not row:
            return False
        count = conn.execute("SELECT COUNT(*) FROM script_lines").fetchone()[0]
        return int(count) > 0
    except sqlite3.Error:
        return False


def _migrate_legacy_database_if_needed() -> None:
    """首次启动时尝试从项目目录旧库迁移（桌面/iCloud 上的库可能已损坏）。"""
    _ensure_db_dir()
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5.0)
            try:
                if _db_has_user_data(conn):
                    return
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    if not LEGACY_DB_PATH.exists():
        return

    try:
        shutil.copy2(LEGACY_DB_PATH, DB_PATH)
        for suffix in ("-wal", "-shm"):
            sidecar = LEGACY_DB_PATH.with_name(LEGACY_DB_PATH.name + suffix)
            if sidecar.exists() and sidecar.stat().st_size > 0:
                shutil.copy2(sidecar, DB_PATH.with_name(DB_PATH.name + suffix))
    except OSError:
        return

    recover_sqlite_wal_files()


def _remove_sqlite_sidecars() -> bool:
    removed = False
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = DB_PATH.with_name(DB_PATH.name + suffix)
        if sidecar.exists():
            try:
                sidecar.unlink()
                removed = True
            except OSError:
                pass
    return removed


def recover_sqlite_wal_files() -> bool:
    """
    从 WAL 异常恢复：先尝试切回 DELETE 并 checkpoint，再删除 -wal/-shm。
    （勿在每次 get_connection 时调用，避免与录制线程并发时触发 disk I/O。）
    """
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
            try:
                conn.execute("PRAGMA busy_timeout = 5000")
                conn.execute("PRAGMA journal_mode = DELETE")
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    return _remove_sqlite_sidecars()


def _prepare_db_files_before_open() -> None:
    """打开库前清理损坏的 0 字节 WAL 等旁路文件。"""
    wal = DB_PATH.with_name(DB_PATH.name + "-wal")
    if wal.exists():
        try:
            if wal.stat().st_size == 0:
                _remove_sqlite_sidecars()
        except OSError:
            _remove_sqlite_sidecars()


def _configure_sqlite_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 60000")
    # 固定 DELETE 日志；WAL 在部分 Mac 环境易出现 disk I/O（尤其 -wal 损坏时）
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA synchronous = NORMAL")


def get_connection() -> sqlite3.Connection:
    _ensure_db_dir()
    _prepare_db_files_before_open()
    last_err: BaseException | None = None
    for attempt in range(8):
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
            conn.row_factory = sqlite3.Row
            _configure_sqlite_pragmas(conn)
            return conn
        except sqlite3.OperationalError as exc:
            last_err = exc
            if _is_database_locked(exc) and attempt < 7:
                time.sleep(0.35 * (attempt + 1))
                continue
            if _is_disk_io_error(exc) and attempt < 7:
                recover_sqlite_wal_files()
                time.sleep(0.5 * (attempt + 1))
                continue
            raise
    if last_err:
        raise last_err
    raise RuntimeError("get_connection failed")


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS characters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            gender TEXT NOT NULL DEFAULT '',
            age TEXT NOT NULL DEFAULT '',
            personality TEXT NOT NULL DEFAULT '',
            quote_1 TEXT NOT NULL DEFAULT '',
            quote_2 TEXT NOT NULL DEFAULT '',
            quote_1_instruction TEXT NOT NULL DEFAULT '',
            quote_2_instruction TEXT NOT NULL DEFAULT '',
            voice_id TEXT NOT NULL DEFAULT '',
            importance_level TEXT NOT NULL DEFAULT 'pending'
        );

        CREATE TABLE IF NOT EXISTS script_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chapter_num INTEGER NOT NULL DEFAULT 1,
            line_idx INTEGER NOT NULL,
            role TEXT NOT NULL,
            voice_id TEXT NOT NULL DEFAULT '',
            emotion_instruction TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            is_dialogue INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_script_lines_chapter
            ON script_lines (chapter_num, line_idx);

        CREATE TABLE IF NOT EXISTS pipeline_checkpoints (
            novel_fingerprint TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            chunk_config TEXT NOT NULL,
            char_start INTEGER NOT NULL,
            char_end INTEGER NOT NULL,
            script_lines_added INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (novel_fingerprint, chunk_index)
        );

        CREATE TABLE IF NOT EXISTS blocked_script_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            novel_fingerprint TEXT NOT NULL,
            chapter_num INTEGER NOT NULL,
            char_start INTEGER NOT NULL,
            char_end INTEGER NOT NULL,
            snippet TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT 'censorship_blocked',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (novel_fingerprint, chapter_num, char_start, char_end)
        );

        CREATE INDEX IF NOT EXISTS idx_blocked_segments_book
            ON blocked_script_segments (novel_fingerprint, chapter_num, status);
        """
    )
    conn.commit()


def reset_database() -> None:
    """Drop and recreate all tables (new novel upload or full re-parse)."""
    recover_sqlite_wal_files()
    if DB_PATH.exists():
        DB_PATH.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = DB_PATH.with_name(DB_PATH.name + suffix)
        if sidecar.exists():
            try:
                sidecar.unlink()
            except OSError:
                pass
    with get_connection() as conn:
        _run_schema_migrations(conn)


def _run_schema_migrations(conn: sqlite3.Connection) -> None:
    init_schema(conn)
    _migrate_characters_schema(conn)
    _migrate_blocked_segments_schema(conn)
    _migrate_script_lines_audio_tracking(conn)


def backup_database(tag: str = "manual") -> Path | None:
    """复制当前库到同目录备份（仅当库内已有剧本数据时）。"""
    _ensure_db_dir()
    if not DB_PATH.exists() or DB_PATH.stat().st_size < 65536:
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM script_lines"
            ).fetchone()
            if not row or int(row[0]) == 0:
                return None
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = DB_DIR / f"b2a_studio.db.{tag}-backup-{stamp}"
    shutil.copy2(DB_PATH, dest)
    return dest


def ensure_database() -> None:
    """Create database file and tables if missing; apply pending schema migrations."""
    global _DB_INITIALIZED
    _migrate_legacy_database_if_needed()
    last_err: BaseException | None = None
    for attempt in range(12):
        try:
            with get_connection() as conn:
                if not _DB_INITIALIZED:
                    backup_database(tag="auto")
                _run_schema_migrations(conn)
            _DB_INITIALIZED = True
            return
        except sqlite3.OperationalError as exc:
            last_err = exc
            if _is_database_locked(exc) and attempt < 11:
                time.sleep(0.4 * (attempt + 1))
                continue
            if _is_disk_io_error(exc) and attempt < 11:
                recover_sqlite_wal_files()
                time.sleep(0.5 * (attempt + 1))
                continue
            if _is_disk_io_error(exc):
                raise RuntimeError(
                    "数据库 disk I/O 错误，已尝试修复 WAL 旁路文件但未成功。"
                    "请勿删除数据库文件。请到 "
                    f"{DB_DIR} 查找 b2a_studio.db.*-backup-* 备份，"
                    "或运行 python recover_library.py 从 CSV/缓存恢复。"
                ) from exc
            raise
    if last_err:
        raise last_err


def database_storage_path() -> Path:
    """供界面提示：剧本库实际存放路径（不在 iCloud 桌面项目内）。"""
    return DB_PATH


def _migrate_characters_schema(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(characters)")}
    if "importance_level" not in cols:
        conn.execute(
            """
            ALTER TABLE characters
            ADD COLUMN importance_level TEXT NOT NULL DEFAULT 'pending'
            """
        )
        conn.commit()


def _migrate_blocked_segments_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'blocked_script_segments'
        """
    ).fetchone()
    if row:
        return
    conn.executescript(
        """
        CREATE TABLE blocked_script_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            novel_fingerprint TEXT NOT NULL,
            chapter_num INTEGER NOT NULL,
            char_start INTEGER NOT NULL,
            char_end INTEGER NOT NULL,
            snippet TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT 'censorship_blocked',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (novel_fingerprint, chapter_num, char_start, char_end)
        );
        CREATE INDEX idx_blocked_segments_book
            ON blocked_script_segments (novel_fingerprint, chapter_num, status);
        """
    )
    conn.commit()


RECORDING_STATUS_OK = "ok"
RECORDING_STATUS_FAILED = "failed"

_AUDIO_TRACKING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("actual_voice_id", "TEXT NOT NULL DEFAULT ''"),
    ("audio_duration", "REAL"),
    ("gap_duration", "REAL"),
    ("start_time_offset", "REAL"),
    ("end_time_offset", "REAL"),
    ("recording_status", "TEXT NOT NULL DEFAULT ''"),
    ("recording_error", "TEXT NOT NULL DEFAULT ''"),
)


def _migrate_script_lines_audio_tracking(conn: sqlite3.Connection) -> None:
    """为 script_lines 追加有声书时间轴追踪字段（已存在则跳过）。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(script_lines)")}
    changed = False
    for col_name, col_def in _AUDIO_TRACKING_COLUMNS:
        if col_name in cols:
            continue
        conn.execute(
            f"ALTER TABLE script_lines ADD COLUMN {col_name} {col_def}"
        )
        changed = True
    if changed:
        conn.commit()
    conn.execute(
        """
        UPDATE script_lines
        SET recording_status = ?
        WHERE TRIM(COALESCE(actual_voice_id, '')) != ''
          AND (recording_status IS NULL OR recording_status = '')
        """,
        (RECORDING_STATUS_OK,),
    )
    conn.commit()


def list_characters(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM characters ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return [dict(row) for row in rows]


def upsert_character(conn: sqlite3.Connection, data: dict[str, Any]) -> None:
    name = (data.get("name") or "").strip()
    if not name or name == "旁白":
        return
    if any(ch in name for ch in "*#@/\\|<>{}[]") or len(name) > 8:
        return

    existing = conn.execute(
        "SELECT * FROM characters WHERE name = ?", (name,)
    ).fetchone()

    merged = {field: "" for field in CHARACTER_FIELDS}
    if existing:
        merged.update(dict(existing))

    for field in CHARACTER_FIELDS:
        if field == "name":
            merged["name"] = name
            continue
        incoming = data.get(field)
        if incoming is None:
            continue
        incoming_str = str(incoming).strip()
        if not incoming_str:
            continue
        if field == "personality" and incoming_str:
            merged[field] = incoming_str
        elif field != "personality":
            merged[field] = incoming_str

    conn.execute(
        """
        INSERT INTO characters (
            name, gender, age, personality,
            quote_1, quote_2, quote_1_instruction, quote_2_instruction,
            voice_id, importance_level
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            gender = excluded.gender,
            age = excluded.age,
            personality = excluded.personality,
            quote_1 = excluded.quote_1,
            quote_2 = excluded.quote_2,
            quote_1_instruction = excluded.quote_1_instruction,
            quote_2_instruction = excluded.quote_2_instruction,
            voice_id = CASE
                WHEN excluded.voice_id != '' THEN excluded.voice_id
                ELSE characters.voice_id
            END,
            importance_level = characters.importance_level
        """,
        tuple(merged[field] for field in CHARACTER_FIELDS) + (IMPORTANCE_PENDING,),
    )


def refresh_rolling_character_ranks(
    conn: sqlite3.Connection,
    *,
    top_n: int = ROLLING_RANK_TOP_N,
) -> dict[str, int]:
    """
    Top N 滚动流式分流：按累计对白行数（is_dialogue=1，排除旁白）排名，
    前 top_n 名 importance_level='main'，其余 'extra'；不足 top_n 人则全员 main。
    """
    count_rows = conn.execute(
        """
        SELECT role, COUNT(*) AS dialogue_cnt
        FROM script_lines
        WHERE is_dialogue = 1
          AND role IS NOT NULL
          AND TRIM(role) != ''
          AND role != '旁白'
        GROUP BY role
        """
    ).fetchall()
    script_roles = {str(r["role"]).strip() for r in count_rows}
    role_line_counts = {str(r["role"]): int(r["dialogue_cnt"]) for r in count_rows}

    from utils.extra_stock import STOCK_EXTRA_NAMES

    rank_exclude = {NARRATOR_NAME, *STOCK_EXTRA_NAMES}
    cast_rows = conn.execute(
        "SELECT name FROM characters WHERE name != ?",
        (NARRATOR_NAME,),
    ).fetchall()
    cast_scores: list[tuple[int, str]] = []
    for row in cast_rows:
        name = (row["name"] or "").strip()
        if not name or name in rank_exclude:
            continue
        cast_scores.append(
            (
                count_dialogue_lines_for_cast(
                    conn,
                    name,
                    script_roles=script_roles,
                    role_line_counts=role_line_counts,
                ),
                name,
            )
        )
    cast_scores.sort(key=lambda x: (-x[0], x[1]))
    ranked_names = [name for score, name in cast_scores if score > 0]
    n_speakers = len(ranked_names)
    if n_speakers == 0:
        return {"main": 0, "extra": 0, "ranked_speakers": 0}

    main_names = set(ranked_names[: min(top_n, n_speakers)])

    all_chars = conn.execute(
        "SELECT name, importance_level FROM characters WHERE name != ?",
        (NARRATOR_NAME,),
    ).fetchall()
    main_count = 0
    extra_count = 0
    for row in all_chars:
        name = (row["name"] or "").strip()
        if not name or name in rank_exclude:
            continue
        if (row["importance_level"] or "").strip() == IMPORTANCE_STOCK:
            continue
        level = IMPORTANCE_MAIN if name in main_names else IMPORTANCE_EXTRA
        conn.execute(
            "UPDATE characters SET importance_level = ? WHERE name = ?",
            (level, name),
        )
        if level == IMPORTANCE_MAIN:
            main_count += 1
        else:
            extra_count += 1

    return {
        "main": main_count,
        "extra": extra_count,
        "ranked_speakers": n_speakers,
    }


def get_importance_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """统计演员表 main / extra / pending 人数。"""
    rows = conn.execute(
        """
        SELECT importance_level, COUNT(*) AS cnt
        FROM characters
        WHERE name != '旁白'
        GROUP BY importance_level
        """
    ).fetchall()
    stats = {"main": 0, "extra": 0, "pending": 0, "total": 0}
    for row in rows:
        level = (row["importance_level"] or IMPORTANCE_PENDING).strip()
        cnt = int(row["cnt"])
        stats["total"] += cnt
        if level == IMPORTANCE_MAIN:
            stats["main"] = cnt
        elif level == IMPORTANCE_EXTRA:
            stats["extra"] = cnt
        else:
            stats["pending"] += cnt
    return stats


def insert_script_lines(
    conn: sqlite3.Connection,
    lines: list[dict[str, Any]],
    *,
    chapter_num: int,
    start_line_idx: int,
) -> int:
    """Append script lines; returns next line_idx."""
    line_idx = start_line_idx
    for row in lines:
        content = (row.get("content") or "").strip()
        if not content:
            continue
        role = (row.get("role") or "旁白").strip() or "旁白"
        is_dialogue = 1 if row.get("is_dialogue") else 0
        conn.execute(
            """
            INSERT INTO script_lines (
                chapter_num, line_idx, role, voice_id,
                emotion_instruction, content, is_dialogue
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chapter_num,
                line_idx,
                role,
                (row.get("voice_id") or "").strip(),
                (row.get("emotion_instruction") or "").strip(),
                content,
                is_dialogue,
            ),
        )
        line_idx += 1
    return line_idx


def get_pipeline_stats(conn: sqlite3.Connection) -> dict[str, int]:
    char_count = conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
    line_count = conn.execute("SELECT COUNT(*) FROM script_lines").fetchone()[0]
    chapter_count = conn.execute(
        "SELECT COUNT(DISTINCT chapter_num) FROM script_lines"
    ).fetchone()[0]
    return {
        "characters": char_count,
        "script_lines": line_count,
        "chapters": chapter_count,
    }


def fetch_chapter_script_content(conn: sqlite3.Connection, chapter_num: int) -> str:
    """按 line_idx 顺序拼接某章全部剧本行 content。"""
    rows = conn.execute(
        """
        SELECT content FROM script_lines
        WHERE chapter_num = ?
        ORDER BY line_idx
        """,
        (chapter_num,),
    ).fetchall()
    return "".join(str(row[0] or "") for row in rows)


def list_script_chapters(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT chapter_num
        FROM script_lines
        ORDER BY chapter_num
        """
    ).fetchall()
    return [int(r[0]) for r in rows]


def fetch_script_lines_preview(
    conn: sqlite3.Connection,
    *,
    chapter_num: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """返回剧本行；chapter_num 指定时仅该章；limit 为 None 时不截断。"""
    sql = """
        SELECT chapter_num, line_idx, role, emotion_instruction,
               content, is_dialogue
        FROM script_lines
    """
    params: list[Any] = []
    if chapter_num is not None:
        sql += " WHERE chapter_num = ?"
        params.append(chapter_num)
    sql += " ORDER BY chapter_num, line_idx"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def delete_characters_by_names(conn: sqlite3.Connection, names: list[str]) -> int:
    if not names:
        return 0
    cur = conn.executemany(
        "DELETE FROM characters WHERE name = ?",
        [(n,) for n in names],
    )
    return cur.rowcount if cur.rowcount is not None else len(names)


def script_line_roles(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT role FROM script_lines
        WHERE role IS NOT NULL AND role != '' AND role != '旁白'
        """
    ).fetchall()
    return {str(r[0]).strip() for r in rows}


def novel_has_pipeline_progress(
    conn: sqlite3.Connection,
    *fingerprints: str,
) -> bool:
    """本地是否已有与给定指纹之一匹配的检查点。"""
    for fp in fingerprints:
        if not fp:
            continue
        row = conn.execute(
            "SELECT 1 FROM pipeline_checkpoints WHERE novel_fingerprint = ? LIMIT 1",
            (fp,),
        ).fetchone()
        if row:
            return True
    return False


def database_has_other_pipeline_progress(
    conn: sqlite3.Connection,
    *fingerprints: str,
) -> bool:
    """库内是否存在与当前书指纹无关的其它断点（换书上传时需清库）。"""
    fps = {fp for fp in fingerprints if fp}
    rows = conn.execute(
        "SELECT DISTINCT novel_fingerprint FROM pipeline_checkpoints"
    ).fetchall()
    for row in rows:
        stored = str(row[0] or "")
        if stored not in fps:
            return True
    return False


def clear_checkpoints(
    conn: sqlite3.Connection,
    novel_fingerprint: str | None = None,
) -> None:
    if novel_fingerprint:
        conn.execute(
            "DELETE FROM pipeline_checkpoints WHERE novel_fingerprint = ?",
            (novel_fingerprint,),
        )
    else:
        conn.execute("DELETE FROM pipeline_checkpoints")
    conn.commit()


def clear_chunk_checkpoint(
    conn: sqlite3.Connection,
    novel_fingerprint: str,
    chunk_index: int,
) -> None:
    """清除单章断点，便于指定章节重新拆解。"""
    conn.execute(
        """
        DELETE FROM pipeline_checkpoints
        WHERE novel_fingerprint = ? AND chunk_index = ?
        """,
        (novel_fingerprint, chunk_index),
    )


def clear_blocked_segments_for_chapter(
    conn: sqlite3.Connection,
    novel_fingerprint: str,
    chapter_num: int,
) -> None:
    conn.execute(
        """
        DELETE FROM blocked_script_segments
        WHERE novel_fingerprint = ? AND chapter_num = ?
        """,
        (novel_fingerprint, chapter_num),
    )


def get_completed_chunk_indices(
    conn: sqlite3.Connection,
    novel_fingerprint: str,
    chunk_config: str,
) -> set[int]:
    rows = conn.execute(
        """
        SELECT chunk_index, chunk_config
        FROM pipeline_checkpoints
        WHERE novel_fingerprint = ?
        ORDER BY chunk_index
        """,
        (novel_fingerprint,),
    ).fetchall()
    done: set[int] = set()
    for row in rows:
        if row["chunk_config"] != chunk_config:
            continue
        done.add(int(row["chunk_index"]))
    return done


def get_completed_chunk_indices_relaxed(
    conn: sqlite3.Connection,
    novel_fingerprint: str,
) -> set[int]:
    """忽略 chunk_config 差异，仅按指纹合并检查点（跳过时会再校验字符区间）。"""
    rows = conn.execute(
        """
        SELECT chunk_index
        FROM pipeline_checkpoints
        WHERE novel_fingerprint = ?
        ORDER BY chunk_index
        """,
        (novel_fingerprint,),
    ).fetchall()
    return {int(row[0]) for row in rows}


def list_checkpoint_fingerprints(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT novel_fingerprint FROM pipeline_checkpoints ORDER BY 1"
    ).fetchall()
    return [str(row[0]) for row in rows]


def is_chunk_checkpoint_valid(
    conn: sqlite3.Connection,
    novel_fingerprint: str,
    chunk_index: int,
    chunk_config: str,
    char_start: int,
    char_end: int,
) -> bool:
    row = conn.execute(
        """
        SELECT chunk_config, char_start, char_end
        FROM pipeline_checkpoints
        WHERE novel_fingerprint = ? AND chunk_index = ?
        """,
        (novel_fingerprint, chunk_index),
    ).fetchone()
    if not row:
        return False
    return (
        row["chunk_config"] == chunk_config
        and int(row["char_start"]) == char_start
        and int(row["char_end"]) == char_end
    )


def mark_chunk_completed(
    conn: sqlite3.Connection,
    *,
    novel_fingerprint: str,
    chunk_index: int,
    chunk_config: str,
    char_start: int,
    char_end: int,
    script_lines_added: int,
) -> None:
    conn.execute(
        """
        INSERT INTO pipeline_checkpoints (
            novel_fingerprint, chunk_index, chunk_config,
            char_start, char_end, script_lines_added, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(novel_fingerprint, chunk_index) DO UPDATE SET
            chunk_config = excluded.chunk_config,
            char_start = excluded.char_start,
            char_end = excluded.char_end,
            script_lines_added = excluded.script_lines_added,
            completed_at = excluded.completed_at
        """,
        (
            novel_fingerprint,
            chunk_index,
            chunk_config,
            char_start,
            char_end,
            script_lines_added,
        ),
    )


def rebuild_chapter_line_idx(conn: sqlite3.Connection) -> dict[int, int]:
    """Resume 时从已有剧本行恢复各章下一 line_idx。"""
    idx: dict[int, int] = {}
    rows = conn.execute(
        """
        SELECT chapter_num, MAX(line_idx) AS mx
        FROM script_lines
        GROUP BY chapter_num
        """
    ).fetchall()
    for row in rows:
        idx[int(row["chapter_num"])] = int(row["mx"]) + 1
    return idx


def script_line_roles_with_dialogue(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT role FROM script_lines
        WHERE is_dialogue = 1
          AND role IS NOT NULL
          AND TRIM(role) != ''
          AND role != '旁白'
        """
    ).fetchall()
    return {str(row[0]).strip() for row in rows}


def script_role_belongs_to_cast(
    cast_name: str,
    script_role: str,
    script_roles: set[str],
    *,
    all_cast_names: set[str] | None = None,
) -> bool:
    """
    剧本 role 与演员表 name 对齐：完全匹配，或简称/职业称呼（如 老板 → 民宿老板）。
    若文中同时存在 老板 与 面馆老板，裸 老板 归入更长人设名且未单独作 role 的演员。
    """
    cast_name = (cast_name or "").strip()
    script_role = (script_role or "").strip()
    if not cast_name or not script_role:
        return False
    if cast_name == script_role:
        if all_cast_names and script_role == "老板":
            specialized = [
                n
                for n in all_cast_names
                if len(n) > len(script_role)
                and n.endswith(script_role)
                and n not in script_roles
            ]
            if specialized:
                return False
        return True
    if len(script_role) < 2 or not cast_name.endswith(script_role):
        return False
    if cast_name in script_roles:
        return False
    longer_roles = [
        r
        for r in script_roles
        if len(r) > len(script_role) and r.endswith(script_role)
    ]
    if not longer_roles:
        return True
    if script_role == "老板" and cast_name.endswith("老板"):
        return cast_name not in script_roles
    return False


def count_dialogue_lines_for_cast(
    conn: sqlite3.Connection,
    cast_name: str,
    *,
    script_roles: set[str] | None = None,
    role_line_counts: dict[str, int] | None = None,
) -> int:
    """累计某演员的对白行数（含剧本 role 简称与演员表 name 不一致的情况）。"""
    cast_name = (cast_name or "").strip()
    if not cast_name:
        return 0
    roles = script_roles if script_roles is not None else script_line_roles_with_dialogue(
        conn
    )
    all_cast_names = {
        str(r[0]).strip()
        for r in conn.execute("SELECT name FROM characters WHERE name != '旁白'").fetchall()
        if str(r[0]).strip()
    }
    if role_line_counts is None:
        rows = conn.execute(
            """
            SELECT role, COUNT(*) AS cnt
            FROM script_lines
            WHERE is_dialogue = 1
              AND role IS NOT NULL
              AND TRIM(role) != ''
              AND role != '旁白'
            GROUP BY role
            """
        ).fetchall()
        role_line_counts = {str(r["role"]): int(r["cnt"]) for r in rows}
    total = 0
    for role, cnt in role_line_counts.items():
        if script_role_belongs_to_cast(
            cast_name, role, roles, all_cast_names=all_cast_names
        ):
            total += cnt
    return total


def fetch_characters_preview(
    conn: sqlite3.Connection,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """演员表预览：按累计对白行数（与 Top14 排名一致）从高到低排序。"""
    sql = """
        SELECT
            c.name,
            c.gender,
            c.age,
            c.personality,
            c.quote_1,
            c.quote_2,
            c.quote_1_instruction,
            c.quote_2_instruction,
            c.voice_id
        FROM characters c
        WHERE c.name != '旁白'
        ORDER BY c.name COLLATE NOCASE ASC
    """
    if limit is None:
        rows = conn.execute(sql).fetchall()
    else:
        rows = conn.execute(sql + " LIMIT ?", (limit,)).fetchall()

    script_roles = script_line_roles_with_dialogue(conn)
    count_rows = conn.execute(
        """
        SELECT role, COUNT(*) AS cnt
        FROM script_lines
        WHERE is_dialogue = 1
          AND role IS NOT NULL
          AND TRIM(role) != ''
          AND role != '旁白'
        GROUP BY role
        """
    ).fetchall()
    role_line_counts = {str(r["role"]): int(r["cnt"]) for r in count_rows}

    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["dialogue_lines"] = count_dialogue_lines_for_cast(
            conn,
            item.get("name", ""),
            script_roles=script_roles,
            role_line_counts=role_line_counts,
        )
        out.append(item)
    out.sort(
        key=lambda r: (-int(r.get("dialogue_lines") or 0), str(r.get("name") or ""))
    )
    return out


def replace_blocked_segments_for_chapter(
    conn: sqlite3.Connection,
    novel_fingerprint: str,
    chapter_num: int,
    segments: list[dict[str, Any]],
) -> int:
    """写入一章的待手动段落（先删该章旧记录）。"""
    conn.execute(
        """
        DELETE FROM blocked_script_segments
        WHERE novel_fingerprint = ? AND chapter_num = ? AND status = 'pending'
        """,
        (novel_fingerprint, chapter_num),
    )
    n = 0
    for seg in segments:
        snippet = (seg.get("snippet") or "").strip()
        if not snippet:
            continue
        conn.execute(
            """
            INSERT INTO blocked_script_segments (
                novel_fingerprint, chapter_num, char_start, char_end,
                snippet, reason, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
            ON CONFLICT(novel_fingerprint, chapter_num, char_start, char_end)
            DO UPDATE SET snippet = excluded.snippet, reason = excluded.reason,
                status = 'pending'
            """,
            (
                novel_fingerprint,
                chapter_num,
                int(seg["char_start"]),
                int(seg["char_end"]),
                snippet,
                (seg.get("reason") or "censorship_blocked")[:500],
            ),
        )
        n += 1
    return n


def list_pending_blocked_segments(
    conn: sqlite3.Connection,
    novel_fingerprint: str,
    *,
    chapter_num: int | None = None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT id, chapter_num, char_start, char_end, snippet, reason, created_at
        FROM blocked_script_segments
        WHERE novel_fingerprint = ? AND status = 'pending'
    """
    params: list[Any] = [novel_fingerprint]
    if chapter_num is not None:
        sql += " AND chapter_num = ?"
        params.append(chapter_num)
    sql += " ORDER BY chapter_num, char_start"
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def count_pending_blocked_segments(
    conn: sqlite3.Connection,
    novel_fingerprint: str,
) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM blocked_script_segments
            WHERE novel_fingerprint = ? AND status = 'pending'
            """,
            (novel_fingerprint,),
        ).fetchone()[0]
    )


def resolve_blocked_segment(conn: sqlite3.Connection, segment_id: int) -> None:
    conn.execute(
        "UPDATE blocked_script_segments SET status = 'resolved' WHERE id = ?",
        (segment_id,),
    )


def renumber_chapter_line_idx(conn: sqlite3.Connection, chapter_num: int) -> None:
    rows = conn.execute(
        """
        SELECT id FROM script_lines
        WHERE chapter_num = ?
        ORDER BY line_idx, id
        """,
        (chapter_num,),
    ).fetchall()
    for idx, row in enumerate(rows, start=1):
        conn.execute(
            "UPDATE script_lines SET line_idx = ? WHERE id = ?",
            (idx, int(row[0])),
        )


def insert_script_line_manual(
    conn: sqlite3.Connection,
    *,
    chapter_num: int,
    after_line_idx: int,
    role: str,
    content: str,
    is_dialogue: bool,
    emotion_instruction: str = "",
    voice_id: str = "",
) -> int:
    """在指定行号之后插入一行，并顺延后续 line_idx。返回新行 line_idx。"""
    insert_at = max(0, int(after_line_idx)) + 1
    conn.execute(
        """
        UPDATE script_lines SET line_idx = line_idx + 1
        WHERE chapter_num = ? AND line_idx >= ?
        """,
        (chapter_num, insert_at),
    )
    conn.execute(
        """
        INSERT INTO script_lines (
            chapter_num, line_idx, role, voice_id,
            emotion_instruction, content, is_dialogue
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chapter_num,
            insert_at,
            (role or "旁白").strip() or "旁白",
            (voice_id or "").strip(),
            (emotion_instruction or "").strip(),
            content,
            1 if is_dialogue else 0,
        ),
    )
    renumber_chapter_line_idx(conn, chapter_num)
    return insert_at


def insert_blank_script_line_at(
    conn: sqlite3.Connection,
    *,
    chapter_num: int,
    line_idx: int,
    position: str,
) -> int:
    """
    在章内指定行号的上方或下方插入空白行（旁白、空正文）。
    返回新行的 line_idx。
    """
    anchor = max(1, int(line_idx))
    pos = (position or "below").strip().lower()
    if pos in ("above", "上", "上方"):
        after_line_idx = max(0, anchor - 1)
    else:
        after_line_idx = anchor
    return insert_script_line_manual(
        conn,
        chapter_num=chapter_num,
        after_line_idx=after_line_idx,
        role="旁白",
        content="",
        is_dialogue=False,
    )


def update_script_line_by_id(
    conn: sqlite3.Connection,
    line_id: int,
    fields: dict[str, Any],
) -> None:
    allowed = {
        "role",
        "content",
        "emotion_instruction",
        "voice_id",
        "is_dialogue",
        "line_idx",
        "chapter_num",
    }
    sets: list[str] = []
    vals: list[Any] = []
    for key, val in fields.items():
        if key not in allowed:
            continue
        if key == "is_dialogue":
            val = 1 if val else 0
        sets.append(f"{key} = ?")
        vals.append(val)
    if not sets:
        return
    vals.append(line_id)
    conn.execute(
        f"UPDATE script_lines SET {', '.join(sets)} WHERE id = ?",
        vals,
    )


def delete_script_line_by_id(
    conn: sqlite3.Connection,
    line_id: int,
    *,
    chapter_num: int | None = None,
) -> int | None:
    row = conn.execute(
        "SELECT chapter_num FROM script_lines WHERE id = ?",
        (line_id,),
    ).fetchone()
    if not row:
        return None
    ch = int(row[0])
    if chapter_num is not None and ch != chapter_num:
        return None
    conn.execute("DELETE FROM script_lines WHERE id = ?", (line_id,))
    renumber_chapter_line_idx(conn, ch)
    return ch


def delete_script_line_by_chapter_line_idx(
    conn: sqlite3.Connection,
    chapter_num: int,
    line_idx: int,
) -> int | None:
    """按章内行号 line_idx 删除（用户界面上的「第 N 行」）。"""
    row = conn.execute(
        """
        SELECT id FROM script_lines
        WHERE chapter_num = ? AND line_idx = ?
        """,
        (chapter_num, line_idx),
    ).fetchone()
    if not row:
        return None
    return delete_script_line_by_id(
        conn, int(row[0]), chapter_num=chapter_num
    )


def update_character_by_name(
    conn: sqlite3.Connection,
    name: str,
    fields: dict[str, Any],
) -> None:
    data = {"name": name, **fields}
    upsert_character(conn, data)


def _dominant_script_voice_for_role(conn: sqlite3.Connection, role: str) -> str:
    """从剧本行中取该 role 已写入最多的 voice_id（用于回填演员表）。"""
    row = conn.execute(
        """
        SELECT voice_id, COUNT(*) AS cnt
        FROM script_lines
        WHERE role = ? AND TRIM(COALESCE(voice_id, '')) != ''
        GROUP BY voice_id
        ORDER BY cnt DESC
        LIMIT 1
        """,
        (role,),
    ).fetchone()
    return str(row["voice_id"]).strip() if row else ""


def resolve_cast_voice_id(
    conn: sqlite3.Connection,
    character_name: str,
    *,
    voice_id_from_row: str = "",
) -> str:
    """演员表 voice_id 优先；旁白若为空则与剧本行已同步音色对齐。"""
    vid = (voice_id_from_row or "").strip()
    if vid:
        return vid
    name = (character_name or "").strip()
    if name != NARRATOR_NAME:
        return ""
    return _dominant_script_voice_for_role(conn, NARRATOR_NAME)


def count_narrator_script_lines(conn: sqlite3.Connection) -> int:
    """剧本中 role=旁白 的行数（含叙述，非对白统计）。"""
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM script_lines
        WHERE role = ? AND TRIM(COALESCE(content, '')) != ''
        """,
        (NARRATOR_NAME,),
    ).fetchone()
    return int(row["cnt"]) if row else 0


def _sample_narrator_preview_lines(
    conn: sqlite3.Connection,
    *,
    limit: int = 2,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT content FROM script_lines
        WHERE role = ? AND TRIM(COALESCE(content, '')) != ''
        ORDER BY LENGTH(content) DESC, chapter_num ASC, line_idx ASC
        LIMIT ?
        """,
        (NARRATOR_NAME, limit),
    ).fetchall()
    return [str(r["content"]).strip() for r in rows if str(r["content"]).strip()]


def ensure_narrator_character(conn: sqlite3.Connection) -> None:
    """确保旁白在演员表中存在（LLM 增量写入会跳过旁白，此处单独维护）。"""
    line_count = count_narrator_script_lines(conn)
    if line_count == 0:
        return

    existing = conn.execute(
        "SELECT * FROM characters WHERE name = ?", (NARRATOR_NAME,)
    ).fetchone()
    merged = {field: "" for field in CHARACTER_FIELDS}
    merged["name"] = NARRATOR_NAME
    if existing:
        merged.update(dict(existing))

    if not str(merged.get("personality") or "").strip():
        merged["personality"] = _NARRATOR_DEFAULT_PERSONALITY
    if not str(merged.get("gender") or "").strip():
        merged["gender"] = "—"
    if not str(merged.get("age") or "").strip():
        merged["age"] = "叙述者"

    samples = _sample_narrator_preview_lines(conn, limit=2)
    if not str(merged.get("quote_1") or "").strip() and samples:
        merged["quote_1"] = samples[0][:280]
    if not str(merged.get("quote_2") or "").strip():
        if len(samples) > 1:
            merged["quote_2"] = samples[1][:280]
        elif samples:
            merged["quote_2"] = samples[0][:280]
    if not str(merged.get("quote_1_instruction") or "").strip():
        merged["quote_1_instruction"] = _NARRATOR_QUOTE_INSTRUCTIONS[0]
    if not str(merged.get("quote_2_instruction") or "").strip():
        merged["quote_2_instruction"] = _NARRATOR_QUOTE_INSTRUCTIONS[1]

    conn.execute(
        """
        INSERT INTO characters (
            name, gender, age, personality,
            quote_1, quote_2, quote_1_instruction, quote_2_instruction,
            voice_id, importance_level
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            gender = CASE
                WHEN TRIM(characters.gender) = '' THEN excluded.gender
                ELSE characters.gender
            END,
            age = CASE
                WHEN TRIM(characters.age) = '' THEN excluded.age
                ELSE characters.age
            END,
            personality = CASE
                WHEN TRIM(characters.personality) = '' THEN excluded.personality
                ELSE characters.personality
            END,
            quote_1 = CASE
                WHEN TRIM(characters.quote_1) = '' THEN excluded.quote_1
                ELSE characters.quote_1
            END,
            quote_2 = CASE
                WHEN TRIM(characters.quote_2) = '' THEN excluded.quote_2
                ELSE characters.quote_2
            END,
            quote_1_instruction = CASE
                WHEN TRIM(characters.quote_1_instruction) = ''
                THEN excluded.quote_1_instruction
                ELSE characters.quote_1_instruction
            END,
            quote_2_instruction = CASE
                WHEN TRIM(characters.quote_2_instruction) = ''
                THEN excluded.quote_2_instruction
                ELSE characters.quote_2_instruction
            END,
            voice_id = CASE
                WHEN TRIM(excluded.voice_id) != '' THEN excluded.voice_id
                ELSE characters.voice_id
            END,
            importance_level = ?
        """,
        (
            *tuple(merged[field] for field in CHARACTER_FIELDS),
            IMPORTANCE_MAIN,
            IMPORTANCE_MAIN,
        ),
    )

    script_voice = _dominant_script_voice_for_role(conn, NARRATOR_NAME)
    if script_voice and not str(merged.get("voice_id") or "").strip():
        conn.execute(
            "UPDATE characters SET voice_id = ? WHERE name = ?",
            (script_voice, NARRATOR_NAME),
        )


def _fetch_narrator_cast_entry(conn: sqlite3.Connection) -> dict[str, Any] | None:
    line_count = count_narrator_script_lines(conn)
    if line_count == 0:
        return None
    row = conn.execute(
        """
        SELECT
            name, gender, age, personality,
            quote_1, quote_2,
            quote_1_instruction, quote_2_instruction,
            voice_id, importance_level
        FROM characters
        WHERE name = ?
        """,
        (NARRATOR_NAME,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["voice_id"] = resolve_cast_voice_id(
        conn, NARRATOR_NAME, voice_id_from_row=str(item.get("voice_id") or "")
    )
    item["dialogue_lines"] = line_count
    item["is_narrator"] = True
    return item


def fetch_main_cast_characters(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """试镜大厅演员：旁白置顶 + importance_level=main 的主演（按对白行数排序）。"""
    ensure_narrator_character(conn)

    rows = conn.execute(
        """
        SELECT
            name, gender, age, personality,
            quote_1, quote_2,
            quote_1_instruction, quote_2_instruction,
            voice_id, importance_level
        FROM characters
        WHERE importance_level = 'main' AND name != ?
        """,
        (NARRATOR_NAME,),
    ).fetchall()
    script_roles = script_line_roles(conn)
    role_line_counts = {
        str(r["role"]): int(r["cnt"])
        for r in conn.execute(
            """
            SELECT role, COUNT(*) AS cnt FROM script_lines
            WHERE is_dialogue = 1 AND role IS NOT NULL AND TRIM(role) != ''
              AND role != ?
            GROUP BY role
            """,
            (NARRATOR_NAME,),
        ).fetchall()
    }
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["voice_id"] = resolve_cast_voice_id(
            conn,
            str(item.get("name") or ""),
            voice_id_from_row=str(item.get("voice_id") or ""),
        )
        item["dialogue_lines"] = count_dialogue_lines_for_cast(
            conn,
            item.get("name", ""),
            script_roles=script_roles,
            role_line_counts=role_line_counts,
        )
        out.append(item)
    out.sort(
        key=lambda r: (
            -int(r.get("dialogue_lines") or 0),
            str(r.get("name") or ""),
        )
    )
    narrator = _fetch_narrator_cast_entry(conn)
    if narrator:
        return [narrator, *out]
    return out


def list_character_voice_assignments(conn: sqlite3.Connection) -> dict[str, str]:
    """voice_id -> 已绑定角色名（防撞音提示用，不强制互斥）。"""
    rows = conn.execute(
        """
        SELECT name, voice_id FROM characters
        WHERE TRIM(voice_id) != ''
        """
    ).fetchall()
    owners: dict[str, str] = {}
    for row in rows:
        vid = str(row["voice_id"] or "").strip()
        name = str(row["name"] or "").strip()
        if vid and name:
            owners[vid] = name
    return owners


def bind_character_voice(
    conn: sqlite3.Connection,
    character_name: str,
    voice_id: str,
) -> dict[str, int]:
    """
    将音色写入 characters.voice_id，并同步到 script_lines 中匹配该演员的角色简称。
    龙套池档位走 extra_stock.bind_stock_extra_voice，自动套用至 extra 配角。
    """
    from utils.extra_stock import bind_stock_extra_voice, is_stock_extra_name

    name = (character_name or "").strip()
    vid = (voice_id or "").strip()
    if not name:
        return {"script_lines_updated": 0}
    if is_stock_extra_name(name):
        result = bind_stock_extra_voice(conn, name, vid)
        return {
            "script_lines_updated": int(result.get("script_lines_updated") or 0),
            "extras_characters_updated": int(result.get("characters_updated") or 0),
            "stock_slot": result.get("stock_slot", name),
        }
    if name == NARRATOR_NAME:
        ensure_narrator_character(conn)
    conn.execute(
        "UPDATE characters SET voice_id = ? WHERE name = ?",
        (vid, name),
    )
    script_roles = {
        str(r[0]).strip()
        for r in conn.execute(
            "SELECT DISTINCT role FROM script_lines WHERE role IS NOT NULL"
        ).fetchall()
        if str(r[0] or "").strip()
    }
    all_cast_names = {
        str(r[0]).strip()
        for r in conn.execute("SELECT name FROM characters").fetchall()
        if str(r[0] or "").strip()
    }
    lines_updated = 0
    for role in script_roles:
        if not script_role_belongs_to_cast(
            name, role, script_roles, all_cast_names=all_cast_names
        ):
            continue
        cur = conn.execute(
            "UPDATE script_lines SET voice_id = ? WHERE role = ?",
            (vid, role),
        )
        if cur.rowcount:
            lines_updated += int(cur.rowcount)
    return {"script_lines_updated": lines_updated}


def fetch_script_lines_for_edit(
    conn: sqlite3.Connection,
    chapter_num: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, chapter_num, line_idx, role, emotion_instruction,
               content, is_dialogue, voice_id,
               actual_voice_id, audio_duration, gap_duration,
               start_time_offset, end_time_offset
        FROM script_lines
        WHERE chapter_num = ?
        ORDER BY line_idx
        """,
        (chapter_num,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_script_lines_for_recording(
    conn: sqlite3.Connection,
    chapter_num: int,
) -> list[dict[str, Any]]:
    """按 id 顺序读取一章剧本行（含时间轴字段）。"""
    rows = conn.execute(
        """
        SELECT id, chapter_num, line_idx, role, emotion_instruction,
               content, is_dialogue, voice_id,
               actual_voice_id, audio_duration, gap_duration,
               start_time_offset, end_time_offset,
               recording_status, recording_error
        FROM script_lines
        WHERE chapter_num = ?
        ORDER BY id
        """,
        (chapter_num,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_character_profile(
    conn: sqlite3.Connection,
    name: str,
) -> dict[str, str]:
    """读取演员 gender/age/voice_id（录制流水线用）。"""
    row = conn.execute(
        """
        SELECT gender, age, voice_id FROM characters WHERE name = ?
        """,
        ((name or "").strip(),),
    ).fetchone()
    if not row:
        return {"gender": "", "age": "", "voice_id": ""}
    return {
        "gender": str(row["gender"] or ""),
        "age": str(row["age"] or ""),
        "voice_id": str(row["voice_id"] or ""),
    }


def update_script_line_audio_tracking(
    conn: sqlite3.Connection,
    line_id: int,
    *,
    actual_voice_id: str,
    audio_duration: float,
    gap_duration: float,
    start_time_offset: float,
    end_time_offset: float,
) -> None:
    conn.execute(
        """
        UPDATE script_lines SET
            actual_voice_id = ?,
            audio_duration = ?,
            gap_duration = ?,
            start_time_offset = ?,
            end_time_offset = ?,
            recording_status = ?,
            recording_error = ''
        WHERE id = ?
        """,
        (
            actual_voice_id,
            float(audio_duration),
            float(gap_duration),
            float(start_time_offset),
            float(end_time_offset),
            RECORDING_STATUS_OK,
            int(line_id),
        ),
    )


def prepare_line_for_rerecord(conn: sqlite3.Connection, line_id: int) -> bool:
    """清除单行录制状态，便于断点续录只重跑该行（无需整章重录）。"""
    cur = conn.execute(
        """
        UPDATE script_lines SET
            recording_status = '',
            recording_error = '',
            actual_voice_id = '',
            audio_duration = NULL,
            gap_duration = NULL,
            start_time_offset = NULL,
            end_time_offset = NULL
        WHERE id = ?
        """,
        (int(line_id),),
    )
    return cur.rowcount > 0


def prepare_failed_line_for_step_retry(
    conn: sqlite3.Connection,
    line_id: int,
) -> bool:
    """断点续录前清除失败标记，使本行重新从 Step 整句/切片开始（含曾 451/Edge 失败）。"""
    cur = conn.execute(
        """
        UPDATE script_lines SET
            recording_status = '',
            recording_error = '',
            actual_voice_id = '',
            audio_duration = NULL,
            gap_duration = NULL,
            start_time_offset = NULL,
            end_time_offset = NULL
        WHERE id = ? AND recording_status = ?
        """,
        (int(line_id), RECORDING_STATUS_FAILED),
    )
    return cur.rowcount > 0


def mark_line_recording_failed(
    conn: sqlite3.Connection,
    line_id: int,
    error_message: str,
) -> None:
    conn.execute(
        """
        UPDATE script_lines SET
            recording_status = ?,
            recording_error = ?,
            actual_voice_id = '',
            audio_duration = NULL,
            gap_duration = NULL,
            start_time_offset = NULL,
            end_time_offset = NULL
        WHERE id = ?
        """,
        (
            RECORDING_STATUS_FAILED,
            (error_message or "unknown")[:800],
            int(line_id),
        ),
    )


def chapter_recording_progress(
    conn: sqlite3.Connection,
    chapter_num: int,
) -> tuple[int, int, int]:
    """返回 (成功行数, 失败行数, 章内总行数)。"""
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(
                CASE WHEN recording_status = ? THEN 1 ELSE 0 END
            ) AS ok_n,
            SUM(
                CASE WHEN recording_status = ? THEN 1 ELSE 0 END
            ) AS failed_n
        FROM script_lines
        WHERE chapter_num = ?
        """,
        (
            RECORDING_STATUS_OK,
            RECORDING_STATUS_FAILED,
            int(chapter_num),
        ),
    ).fetchone()
    total = int(row["total"] or 0) if row else 0
    ok_n = int(row["ok_n"] or 0) if row else 0
    failed_n = int(row["failed_n"] or 0) if row else 0
    return ok_n, failed_n, total


def book_recording_progress_summary(
    conn: sqlite3.Connection,
) -> dict[str, int | None]:
    """
    从库内统计全书录制进度（刷新页面后用于恢复进度条）。

    返回字段：chapter_count, chapters_complete, lines_ok, lines_failed,
    lines_total, active_chapter, active_ok, active_failed, active_total。
    """
    chapters = list_script_chapters(conn)
    chapter_count = len(chapters)
    if chapter_count == 0:
        return {
            "chapter_count": 0,
            "chapters_complete": 0,
            "lines_ok": 0,
            "lines_failed": 0,
            "lines_total": 0,
            "active_chapter": None,
            "active_ok": 0,
            "active_failed": 0,
            "active_total": 0,
        }

    chapters_complete = 0
    lines_ok = 0
    lines_failed = 0
    lines_total = 0
    active_chapter: int | None = None
    active_ok = 0
    active_failed = 0
    active_total = 0

    for ch in chapters:
        ok_n, failed_n, total_n = chapter_recording_progress(conn, ch)
        lines_ok += ok_n
        lines_failed += failed_n
        lines_total += total_n
        if total_n > 0 and ok_n >= total_n and failed_n == 0:
            chapters_complete += 1

    # 当前章：优先「已有录制进度但未完成」的章，避免第 1 章全空时盖住第 3–11 章进度
    for ch in chapters:
        ok_n, failed_n, total_n = chapter_recording_progress(conn, ch)
        if total_n > 0 and (ok_n > 0 or failed_n > 0) and (
            ok_n < total_n or failed_n > 0
        ):
            active_chapter = ch
            active_ok = ok_n
            active_failed = failed_n
            active_total = total_n
            break

    if active_chapter is None:
        for ch in chapters:
            ok_n, failed_n, total_n = chapter_recording_progress(conn, ch)
            if total_n > 0 and (ok_n < total_n or failed_n > 0):
                active_chapter = ch
                active_ok = ok_n
                active_failed = failed_n
                active_total = total_n
                break

    if active_chapter is None and chapters:
        last = chapters[-1]
        ok_n, failed_n, total_n = chapter_recording_progress(conn, last)
        active_chapter = last
        active_ok = ok_n
        active_failed = failed_n
        active_total = total_n

    return {
        "chapter_count": chapter_count,
        "chapters_complete": chapters_complete,
        "lines_ok": lines_ok,
        "lines_failed": lines_failed,
        "lines_total": lines_total,
        "active_chapter": active_chapter,
        "active_ok": active_ok,
        "active_failed": active_failed,
        "active_total": active_total,
    }


def fetch_failed_script_lines(
    conn: sqlite3.Connection,
    chapter_num: int | None = None,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if chapter_num is not None:
        rows = conn.execute(
            """
            SELECT id, chapter_num, line_idx, role,
                   substr(content, 1, 80) AS content_preview,
                   recording_error
            FROM script_lines
            WHERE chapter_num = ? AND recording_status = ?
            ORDER BY line_idx
            LIMIT ?
            """,
            (int(chapter_num), RECORDING_STATUS_FAILED, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, chapter_num, line_idx, role,
                   substr(content, 1, 80) AS content_preview,
                   recording_error
            FROM script_lines
            WHERE recording_status = ?
            ORDER BY chapter_num, line_idx
            LIMIT ?
            """,
            (RECORDING_STATUS_FAILED, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def clear_chapter_audio_tracking(conn: sqlite3.Connection, chapter_num: int) -> None:
    conn.execute(
        """
        UPDATE script_lines SET
            actual_voice_id = '',
            audio_duration = NULL,
            gap_duration = NULL,
            start_time_offset = NULL,
            end_time_offset = NULL,
            recording_status = '',
            recording_error = ''
        WHERE chapter_num = ?
        """,
        (int(chapter_num),),
    )
