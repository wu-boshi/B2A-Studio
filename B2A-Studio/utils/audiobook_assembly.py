"""整章时间轴合拢：行级静音间隙 + pydub 导出 MP3。"""

from __future__ import annotations

import io
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .audiobook_ffmpeg import ensure_ffmpeg_configured, load_mp3_segment

ensure_ffmpeg_configured()

from pydub import AudioSegment
from .audiobook_paths import (
    chapter_mp3_path,
    legacy_chapter_mp3_path,
    line_cache_audio_path,
)
from db import RECORDING_STATUS_FAILED, RECORDING_STATUS_OK, update_script_line_audio_tracking

LogFn = Callable[[str], None]

NARRATION_GAP_SEC = 0.8
DIALOGUE_GAP_SEC = 0.4
SILENCE_PLACEHOLDER_SEC = NARRATION_GAP_SEC
CHAPTER_TITLE_AFTER_GAP_SEC = float(
    os.environ.get("B2A_CHAPTER_TITLE_GAP_SEC", "1.0")
)

_CHAPTER_TITLE_LINE_RE = re.compile(
    r"^第\s*[0-9一二三四五六七八九十百千万零〇两]+\s*章"
)


def gap_seconds_for_line(is_dialogue: bool) -> float:
    return DIALOGUE_GAP_SEC if is_dialogue else NARRATION_GAP_SEC


def is_chapter_title_line(line: dict[str, Any]) -> bool:
    """旁白行且内容为「第N章 …」章标题（与正文首句之间的 1s 停顿）。"""
    if bool(line.get("is_dialogue")):
        return False
    emotion = str(line.get("emotion_instruction") or "")
    if emotion.strip() == "章标题" or any(
        k in emotion for k in ("章名", "章标题", "章节标题", "章节信息")
    ):
        return True
    content = str(line.get("content") or "").strip()
    if not content or not _CHAPTER_TITLE_LINE_RE.match(content):
        return False
    if len(content) > 64:
        return False
    rest = content[_CHAPTER_TITLE_LINE_RE.match(content).end() :].lstrip()
    if not rest:
        return True
    from utils.chapter_title_lines import classify_chapter_opening_tail

    kind, _ = classify_chapter_opening_tail(rest)
    return kind == "title"


def gap_seconds_after_line(line: dict[str, Any]) -> float:
    """行后静音：章标题行后固定 1s，其余按对白/旁白规则。"""
    if is_chapter_title_line(line):
        return CHAPTER_TITLE_AFTER_GAP_SEC
    return gap_seconds_for_line(bool(line.get("is_dialogue")))


def audio_duration_seconds(audio_bytes: bytes) -> float:
    seg = load_mp3_segment(audio_bytes)
    return len(seg) / 1000.0


def make_silence(seconds: float) -> AudioSegment:
    ms = max(0, int(round(seconds * 1000)))
    return AudioSegment.silent(duration=ms)


def make_silence_mp3_bytes(seconds: float = SILENCE_PLACEHOLDER_SEC) -> bytes:
    """生成指定时长的静音 MP3（用于纯标点行占位，时长与旁白行间隙一致）。"""
    buf = io.BytesIO()
    make_silence(seconds).export(buf, format="mp3")
    return buf.getvalue()


def save_line_cache(
    novel_name: str,
    chapter_num: int,
    line_id: int,
    audio_bytes: bytes,
) -> Path:
    path = line_cache_audio_path(novel_name, chapter_num, line_id, ext="mp3")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audio_bytes)
    return path


def assemble_chapter_mp3(
    novel_name: str,
    chapter_num: int,
    line_segments: list[tuple[bytes, float]],
    *,
    lyrics_cues: list[tuple[float, str]] | None = None,
    log: LogFn | None = None,
) -> Path:
    """
    按行顺序合并音频与静音间隙，导出 `[小说名]第NNN章.mp3`（三位章节号）。
    line_segments: [(mp3_bytes, gap_after_sec), ...]
    lyrics_cues: [(start_sec, "角色：台词"), ...] 用于内嵌 SYLT
    """
    write_log = log or (lambda _: None)
    ensure_ffmpeg_configured()
    combined = AudioSegment.empty()
    for idx, (raw, gap) in enumerate(line_segments, 1):
        combined += load_mp3_segment(raw)
        if gap > 0:
            combined += make_silence(gap)
    out_path = chapter_mp3_path(novel_name, chapter_num)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_log(f"📦 正在导出整章 MP3 → {out_path.name}")
    combined.export(str(out_path), format="mp3")
    legacy_mp3 = legacy_chapter_mp3_path(novel_name, chapter_num)
    if legacy_mp3.is_file() and legacy_mp3.resolve() != out_path.resolve():
        try:
            legacy_mp3.unlink()
            legacy_lrc = legacy_mp3.with_suffix(".lrc")
            if legacy_lrc.is_file():
                legacy_lrc.unlink()
        except OSError:
            pass
    if lyrics_cues:
        from utils.mp3_lyrics import apply_chapter_lyrics

        sylt_ok, lrc_ok = apply_chapter_lyrics(out_path, lyrics_cues)
        if sylt_ok and lrc_ok:
            write_log(
                f"📝 第 {chapter_num} 章已写入 SYLT + "
                f"{out_path.with_suffix('.lrc').name}（{len(lyrics_cues)} 条）"
            )
        else:
            if not sylt_ok:
                write_log(f"⚠️ 第 {chapter_num} 章 SYLT 内嵌失败")
            if not lrc_ok:
                write_log(f"⚠️ 第 {chapter_num} 章 LRC 写出失败")
    write_log(f"✅ 第 {chapter_num} 章成品已写入：{out_path}")
    return out_path


def line_has_ok_recording(line: dict, cache_path: Path) -> bool:
    """是否已有可合拢的成功行缓存。"""
    status = str(line.get("recording_status") or "").strip()
    if status == RECORDING_STATUS_FAILED:
        return False
    if not cache_path.is_file():
        return False
    if status == RECORDING_STATUS_OK:
        return True
    return bool(str(line.get("actual_voice_id") or "").strip())


def remove_chapter_mp3_if_exists(novel_name: str, chapter_num: int) -> bool:
    """删除章成品 MP3/LRC（含旧版命名），避免误导试听。"""
    removed = False
    for path in (
        chapter_mp3_path(novel_name, chapter_num),
        legacy_chapter_mp3_path(novel_name, chapter_num),
    ):
        for suffix in (".mp3", ".lrc"):
            candidate = path.with_suffix(suffix)
            if candidate.is_file():
                try:
                    candidate.unlink()
                    removed = True
                except OSError:
                    pass
    return removed


def chapter_lines_fully_recorded(
    lines: list[dict[str, Any]],
    novel_name: str,
    chapter_num: int,
) -> tuple[bool, int, int]:
    """(是否可导出整章, 成功行数, 失败行数)。"""
    if not lines:
        return False, 0, 0
    ok_n = 0
    failed_n = 0
    for line in lines:
        status = str(line.get("recording_status") or "").strip()
        if status == RECORDING_STATUS_FAILED:
            failed_n += 1
            continue
        cache_path = line_cache_audio_path(
            novel_name, chapter_num, int(line["id"])
        )
        if line_has_ok_recording(line, cache_path):
            ok_n += 1
    total = len(lines)
    return ok_n >= total and failed_n == 0, ok_n, failed_n


def reassemble_chapter_from_line_caches(
    novel_name: str,
    chapter_num: int,
    lines: list[dict[str, Any]],
    conn: sqlite3.Connection,
    *,
    log: LogFn | None = None,
) -> tuple[Path | None, int, int]:
    """
    仅当章内每一行均已成功时，才导出整章 MP3；否则删除半成品并只保留行缓存。
    返回 (成品路径或 None, 成功行数, 失败行数)。
    """
    write_log = log or (lambda _: None)
    fully_ok, ok_n, failed_n = chapter_lines_fully_recorded(
        lines, novel_name, chapter_num
    )
    if not fully_ok:
        if remove_chapter_mp3_if_exists(novel_name, chapter_num):
            write_log(
                f"ch{chapter_num} incomplete ({ok_n}/{len(lines)} ok, "
                f"{failed_n} failed); removed partial chapter mp3"
            )
        return None, ok_n, failed_n

    current_time = 0.0
    segments: list[tuple[bytes, float]] = []
    lyrics_cues: list[tuple[float, str]] = []
    from utils.mp3_lyrics import format_sync_lyric_line

    for line in lines:
        line_id = int(line["id"])
        cache_path = line_cache_audio_path(novel_name, chapter_num, line_id)
        if not line_has_ok_recording(line, cache_path):
            continue

        raw = cache_path.read_bytes()
        duration = audio_duration_seconds(raw)
        is_dialogue = bool(line.get("is_dialogue"))
        gap = gap_seconds_after_line(line)
        start_offset = current_time
        end_offset = start_offset + duration
        voice_id = str(line.get("actual_voice_id") or "").strip()
        lyrics_cues.append(
            (
                start_offset,
                format_sync_lyric_line(
                    str(line.get("role") or "旁白"),
                    str(line.get("content") or ""),
                ),
            )
        )

        update_script_line_audio_tracking(
            conn,
            line_id,
            actual_voice_id=voice_id,
            audio_duration=duration,
            gap_duration=gap,
            start_time_offset=start_offset,
            end_time_offset=end_offset,
        )

        segments.append((raw, gap))
        current_time = end_offset + gap

    if not segments:
        return None, 0, failed_n

    out_path = assemble_chapter_mp3(
        novel_name,
        chapter_num,
        segments,
        lyrics_cues=lyrics_cues,
        log=write_log,
    )
    write_log(f"ch{chapter_num} exported {len(segments)} lines -> {out_path.name}")
    return out_path, len(segments), failed_n


def repair_punctuation_failed_lines(novel_name: str) -> int:
    """将库内「纯标点」失败行改为 0.8s 静音并标记成功。"""
    from utils.audiobook_synth import is_punctuation_only_content
    from db import get_connection

    fixed = 0
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, chapter_num, content, is_dialogue
            FROM script_lines
            WHERE recording_status = ?
            """,
            (RECORDING_STATUS_FAILED,),
        ).fetchall()
        for row in rows:
            content = str(row["content"] or "")
            if not is_punctuation_only_content(content):
                continue
            line_id = int(row["id"])
            chapter_num = int(row["chapter_num"])
            is_dialogue = bool(row["is_dialogue"])
            audio = make_silence_mp3_bytes(SILENCE_PLACEHOLDER_SEC)
            save_line_cache(novel_name, chapter_num, line_id, audio)
            duration = audio_duration_seconds(audio)
            gap = gap_seconds_for_line(is_dialogue)
            update_script_line_audio_tracking(
                conn,
                line_id,
                actual_voice_id=f"silence:{SILENCE_PLACEHOLDER_SEC}s",
                audio_duration=duration,
                gap_duration=gap,
                start_time_offset=0.0,
                end_time_offset=0.0,
            )
            fixed += 1
        conn.commit()
    return fixed


def mirror_missing_line_caches(
    novel_name: str,
    line_pairs: list[tuple[int, int]],
) -> int:
    """把其它书名变体目录里对齐的行级 MP3 复制到当前解析目录。"""
    import shutil

    from utils.audiobook_paths import (
        _title_dir_variants,
        audiobook_search_roots,
        resolve_audiobook_output_dir,
    )

    primary = resolve_audiobook_output_dir(novel_name)
    copied = 0
    for root in audiobook_search_roots():
        for variant in _title_dir_variants(novel_name):
            alt = root / f"{variant}_有声书"
            if not alt.is_dir() or alt.resolve() == primary.resolve():
                continue
            for line_id, chapter_num in line_pairs:
                dest = (
                    primary
                    / ".cache"
                    / f"chapter_{int(chapter_num):04d}"
                    / f"line_{int(line_id)}.mp3"
                )
                if dest.is_file() and dest.stat().st_size > 0:
                    continue
                src = (
                    alt
                    / ".cache"
                    / f"chapter_{int(chapter_num):04d}"
                    / f"line_{int(line_id)}.mp3"
                )
                if not src.is_file() or src.stat().st_size <= 0:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                copied += 1
    return copied


def sync_recording_status_from_caches(novel_name: str) -> int:
    """
    根据磁盘行级 MP3 缓存回填 recording_status（库被恢复后目录名不一致时使用）。
    """
    from db import (
        fetch_script_lines_for_recording,
        get_connection,
        list_script_chapters,
        update_script_line_audio_tracking,
    )

    updated = 0
    with get_connection() as conn:
        for ch in list_script_chapters(conn):
            lines = fetch_script_lines_for_recording(conn, ch)
            for line in lines:
                line_id = int(line["id"])
                cache_path = line_cache_audio_path(novel_name, ch, line_id)
                if not cache_path.is_file() or cache_path.stat().st_size <= 0:
                    continue
                status = str(line.get("recording_status") or "").strip()
                if status == RECORDING_STATUS_OK:
                    continue
                raw = cache_path.read_bytes()
                duration = audio_duration_seconds(raw)
                gap = gap_seconds_after_line(line)
                voice = str(line.get("actual_voice_id") or line.get("voice_id") or "")
                update_script_line_audio_tracking(
                    conn,
                    line_id,
                    actual_voice_id=voice,
                    audio_duration=duration,
                    gap_duration=gap,
                    start_time_offset=0.0,
                    end_time_offset=duration,
                )
                updated += 1
        conn.commit()
    return updated


def scan_duration_anomalies(
    novel_name: str,
    *,
    chapter_nums: list[int] | None = None,
) -> list[dict[str, Any]]:
    """扫描已录行中 Step 时长明显偏长的条目（含库内时长与磁盘缓存）。"""
    from utils.audiobook_synth import (
        expected_max_duration_seconds,
        is_abnormal_step_duration,
    )
    from db import get_connection

    rows: list[dict[str, Any]] = []
    with get_connection() as conn:
        if chapter_nums:
            placeholders = ",".join("?" * len(chapter_nums))
            sql = f"""
                SELECT id, chapter_num, line_idx, role, content, audio_duration
                FROM script_lines
                WHERE chapter_num IN ({placeholders})
                  AND recording_status = ?
                  AND audio_duration IS NOT NULL
            """
            params: list[Any] = [*chapter_nums, RECORDING_STATUS_OK]
        else:
            sql = """
                SELECT id, chapter_num, line_idx, role, content, audio_duration
                FROM script_lines
                WHERE recording_status = ?
                  AND audio_duration IS NOT NULL
            """
            params = [RECORDING_STATUS_OK]
        for line in conn.execute(sql, params).fetchall():
            content = str(line["content"] or "").strip()
            if not content:
                continue
            try:
                dur = float(line["audio_duration"])
            except (TypeError, ValueError):
                continue
            if not is_abnormal_step_duration(content, dur):
                continue
            line_id = int(line["id"])
            ch = int(line["chapter_num"])
            expected = expected_max_duration_seconds(content)
            rows.append(
                {
                    "line_id": line_id,
                    "chapter_num": ch,
                    "line_idx": int(line["line_idx"] or 0),
                    "role": str(line["role"] or ""),
                    "content": content[:48],
                    "duration_sec": round(dur, 2),
                    "expected_max_sec": round(expected, 2),
                    "chars": len(content),
                }
            )
        if chapter_nums is None:
            missing_dur = conn.execute(
                """
                SELECT id, chapter_num, line_idx, role, content
                FROM script_lines
                WHERE recording_status = ?
                  AND (audio_duration IS NULL OR audio_duration <= 0)
                """,
                (RECORDING_STATUS_OK,),
            ).fetchall()
        else:
            placeholders = ",".join("?" * len(chapter_nums))
            missing_dur = conn.execute(
                f"""
                SELECT id, chapter_num, line_idx, role, content
                FROM script_lines
                WHERE chapter_num IN ({placeholders})
                  AND recording_status = ?
                  AND (audio_duration IS NULL OR audio_duration <= 0)
                """,
                [*chapter_nums, RECORDING_STATUS_OK],
            ).fetchall()
        for line in missing_dur:
            content = str(line["content"] or "").strip()
            if not content:
                continue
            line_id = int(line["id"])
            ch = int(line["chapter_num"])
            cache_path = line_cache_audio_path(novel_name, ch, line_id)
            if not cache_path.is_file() or cache_path.stat().st_size <= 0:
                continue
            dur = audio_duration_seconds(cache_path.read_bytes())
            if not is_abnormal_step_duration(content, dur):
                continue
            expected = expected_max_duration_seconds(content)
            rows.append(
                {
                    "line_id": line_id,
                    "chapter_num": ch,
                    "line_idx": int(line["line_idx"] or 0),
                    "role": str(line["role"] or ""),
                    "content": content[:48],
                    "duration_sec": round(dur, 2),
                    "expected_max_sec": round(expected, 2),
                    "chars": len(content),
                }
            )
    return rows


def queue_lines_for_rerecord(
    novel_name: str,
    line_ids: list[int],
) -> int:
    """标记指定行待续录重跑：清库状态、删行缓存、移除对应章成品 MP3。"""
    from db import get_connection, prepare_line_for_rerecord

    if not line_ids:
        return 0
    chapters_touched: set[int] = set()
    queued = 0
    with get_connection() as conn:
        for line_id in line_ids:
            row = conn.execute(
                "SELECT chapter_num FROM script_lines WHERE id = ?",
                (int(line_id),),
            ).fetchone()
            if not row:
                continue
            ch = int(row["chapter_num"])
            if not prepare_line_for_rerecord(conn, int(line_id)):
                continue
            cache_path = line_cache_audio_path(novel_name, ch, int(line_id))
            if cache_path.is_file():
                try:
                    cache_path.unlink()
                except OSError:
                    pass
            chapters_touched.add(ch)
            queued += 1
        conn.commit()
    for ch in chapters_touched:
        remove_chapter_mp3_if_exists(novel_name, ch)
    return queued


def scan_and_queue_duration_anomalies(novel_name: str) -> tuple[list[dict[str, Any]], int]:
    """扫描时长异常行并加入续录队列（不触发合成，需用户断点续录）。"""
    found = scan_duration_anomalies(novel_name)
    queued = queue_lines_for_rerecord(
        novel_name, [int(r["line_id"]) for r in found]
    )
    return found, queued


def quick_recording_library_sync(novel_name: str) -> tuple[int, int, int, int]:
    """打开录音棚时的轻量同步：磁盘缓存↔库、修标点失败行、删未完成章 MP3、补全已完成章成品。"""
    from utils.audiobook_paths import refresh_audiobook_output_dir_resolution
    from utils.role_voice import sync_orphan_script_role_voices
    from db import (
        fetch_script_lines_for_recording,
        get_connection,
        list_script_chapters,
    )

    with get_connection() as conn:
        pairs = [
            (int(r["id"]), int(r["chapter_num"]))
            for r in conn.execute(
                "SELECT id, chapter_num FROM script_lines"
            ).fetchall()
        ]
        role_sync = sync_orphan_script_role_voices(conn)
        conn.commit()
    refresh_audiobook_output_dir_resolution(novel_name, pairs)
    mirror_missing_line_caches(novel_name, pairs)
    sync_recording_status_from_caches(novel_name)
    fixed = repair_punctuation_failed_lines(novel_name)
    removed = prune_incomplete_chapter_mp3s(novel_name)
    reassembled = _reassemble_complete_chapters_missing_mp3(novel_name)
    _, duration_queued = scan_and_queue_duration_anomalies(novel_name)
    return fixed, removed, reassembled, duration_queued


def _reassemble_complete_chapters_missing_mp3(novel_name: str) -> int:
    """行已全部成功但章 MP3 缺失时，后台自动合拢（替代手动重建按钮）。"""
    from db import fetch_script_lines_for_recording, get_connection, list_script_chapters

    done = 0
    with get_connection() as conn:
        for ch in list_script_chapters(conn):
            mp3 = chapter_mp3_path(novel_name, ch)
            if mp3.is_file():
                continue
            lines = fetch_script_lines_for_recording(conn, ch)
            fully_ok, _, _ = chapter_lines_fully_recorded(
                lines, novel_name, ch
            )
            if not fully_ok:
                continue
            path, _, _ = reassemble_chapter_from_line_caches(
                novel_name, ch, lines, conn
            )
            if path:
                done += 1
        conn.commit()
    return done


def reassemble_all_complete_chapters(
    novel_name: str,
    *,
    log: LogFn | None = None,
) -> tuple[int, int]:
    """手动重建所有已 100% 完成行的章节 MP3（耗时，勿在页面加载时自动跑）。"""
    from db import fetch_script_lines_for_recording, get_connection, list_script_chapters

    write_log = log or (lambda _: None)
    done = 0
    skipped = 0
    with get_connection() as conn:
        for ch in list_script_chapters(conn):
            lines = fetch_script_lines_for_recording(conn, ch)
            fully_ok, _, _ = chapter_lines_fully_recorded(lines, novel_name, ch)
            if not fully_ok:
                skipped += 1
                continue
            path, _, _ = reassemble_chapter_from_line_caches(
                novel_name, ch, lines, conn, log=write_log
            )
            if path:
                done += 1
        conn.commit()
    return done, skipped


def prune_incomplete_chapter_mp3s(novel_name: str) -> int:
    """启动录制或打开面板时，移除未 100% 完成的章成品 MP3。"""
    from db import fetch_script_lines_for_recording, get_connection, list_script_chapters

    removed = 0
    with get_connection() as conn:
        for ch in list_script_chapters(conn):
            lines = fetch_script_lines_for_recording(conn, ch)
            fully_ok, _, _ = chapter_lines_fully_recorded(
                lines, novel_name, ch
            )
            if not fully_ok and remove_chapter_mp3_if_exists(novel_name, ch):
                removed += 1
    return removed
