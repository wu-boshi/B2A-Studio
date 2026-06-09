"""全书有声书录制编排（线程安全、断点友好）。"""

from __future__ import annotations

import traceback
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .audiobook_assembly import (
    audio_duration_seconds,
    chapter_lines_fully_recorded,
    gap_seconds_after_line,
    line_has_ok_recording,
    prune_incomplete_chapter_mp3s,
    reassemble_chapter_from_line_caches,
    save_line_cache,
)
from .audiobook_ffmpeg import FFmpegNotAvailable, ensure_ffmpeg_configured
from .audiobook_paths import (
    audiobook_output_dir,
    chapter_cache_dir,
    resolve_chapter_mp3_path,
    line_cache_audio_path,
)
from .audiobook_synth import (
    EDGE_IMPL,
    LineRecordingFailed,
    RecordingPaused,
    synthesize_line_audio_with_retry,
)
from db import (
    RECORDING_STATUS_FAILED,
    chapter_recording_progress,
    clear_chapter_audio_tracking,
    fetch_script_lines_for_recording,
    get_connection,
    list_script_chapters,
    mark_line_recording_failed,
    prepare_failed_line_for_step_retry,
    update_script_line_audio_tracking,
)
from .recording_log import RecordingDebugLog
from .step_audio import StepAudioError
from .pronunciation import load_confirmed_rules_for_api, tone_rules_for_spoken_text
from .role_voice import resolve_voice_for_script_role

LogFn = Callable[[str], None]

# 连续失败达到该条数（含）即自动暂停全书录制
CONSECUTIVE_LINE_FAIL_PAUSE = 3


@dataclass
class RecordingState:
    running: bool = False
    paused: bool = False
    reset_requested: bool = False
    book_current: int = 0
    book_total: int = 0
    chapter_current: int = 0
    chapter_total: int = 0
    line_current: int = 0
    line_total: int = 0
    status_message: str = ""
    chapter_filter: list[int] | None = None
    completed_chapters: list[int] = field(default_factory=list)
    chapter_mp3_paths: dict[int, str] = field(default_factory=dict)
    log_lines: list[str] = field(default_factory=list)
    last_error: str = ""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _pause_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def __post_init__(self) -> None:
        self._pause_event.set()

    def append_log(self, line: str) -> None:
        with self._lock:
            self.log_lines.append(line)
            if len(self.log_lines) > 800:
                self.log_lines = self.log_lines[-600:]

    def snapshot_logs(self) -> list[str]:
        with self._lock:
            return list(self.log_lines)

    def wait_if_paused(self) -> None:
        while True:
            with self._lock:
                if self.reset_requested or self._stop_event.is_set():
                    raise RecordingPaused("录制已停止")
                paused = self.paused
            if not paused:
                return
            if not self._pause_event.wait(timeout=0.5):
                continue
            with self._lock:
                if not self.paused:
                    self._pause_event.clear()
                    return


def _line_needs_recording(line: dict, cache_path: Path, *, resume: bool) -> bool:
    if not resume:
        return True
    if str(line.get("recording_status") or "").strip() == RECORDING_STATUS_FAILED:
        return True
    return not line_has_ok_recording(line, cache_path)


class AudiobookRecorder:
    def __init__(self) -> None:
        self.state = RecordingState()
        self._thread: threading.Thread | None = None
        self._db_lock = threading.Lock()
        self._consecutive_line_failures = 0
        self._halt_for_consecutive_failures = False
        self._novel_fingerprint = ""
        self._pronunciation_rules: list[dict] = []
        self._dbg = RecordingDebugLog(on_line=self.state.append_log)

    def start(
        self,
        *,
        api_key: str,
        novel_name: str,
        novel_fingerprint: str = "",
        resume: bool = True,
        chapter_nums: list[int] | None = None,
    ) -> None:
        with self.state._lock:
            if self.state.running:
                self._dbg.warn("全书录制", "已在运行中")
                return
            self.state.running = True
            self.state.paused = False
            self.state.reset_requested = False
            self.state.last_error = ""
            self.state.chapter_filter = (
                sorted({int(n) for n in chapter_nums})
                if chapter_nums
                else None
            )
            self.state._stop_event.clear()
            self.state._pause_event.set()
            self._consecutive_line_failures = 0
            self._halt_for_consecutive_failures = False
            self._novel_fingerprint = (novel_fingerprint or "").strip()

        def worker() -> None:
            self._dbg.line("Edge引擎", "配置", EDGE_IMPL)
            try:
                self._run(
                    api_key,
                    novel_name,
                    novel_fingerprint=self._novel_fingerprint,
                    resume=resume,
                    chapter_nums=self.state.chapter_filter,
                )
            except RecordingPaused as exc:
                self._dbg.line("全书录制", "暂停", str(exc) or "用户暂停/停止")
            except Exception as exc:
                detail = traceback.format_exc()
                self.state.last_error = str(exc)
                tail = detail.strip().splitlines()[-1] if detail else str(exc)
                self._dbg.fail(
                    "全书录制",
                    str(exc),
                    "后台线程退出",
                    tail[:200],
                    "重启 Streamlit 后勾选断点续录",
                )
            finally:
                with self.state._lock:
                    self.state.running = False
                    self.state.paused = False

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def pause(self) -> None:
        with self.state._lock:
            if not self.state.running:
                return
            self.state.paused = True
            self.state._pause_event.clear()
            self.state.status_message = "已暂停"
        self._dbg.line("用户操作", "暂停", "全书录制已挂起")

    def resume(self) -> None:
        with self.state._lock:
            if not self.state.running:
                return
            self.state.paused = False
            self.state._pause_event.set()
            self.state.status_message = "继续录制…"
            self._halt_for_consecutive_failures = False
            self._consecutive_line_failures = 0
        self._dbg.line("用户操作", "继续", "连续失败计数已清零")

    def reset(self) -> None:
        with self.state._lock:
            self.state.reset_requested = True
            self.state.paused = False
            self.state._pause_event.set()
            self.state._stop_event.set()
        self._consecutive_line_failures = 0
        self._halt_for_consecutive_failures = False
        self._dbg.line("用户操作", "重置", "请求停止并清空状态")

    def _register_line_success(self) -> None:
        self._consecutive_line_failures = 0

    def _register_line_failure(
        self,
        *,
        chapter_num: int,
        line_idx: int,
        line_id: int,
        error_message: str,
        cache_path: Path,
    ) -> None:
        with self._db_lock:
            with get_connection() as conn:
                mark_line_recording_failed(conn, line_id, error_message)
                conn.commit()
        if cache_path.is_file():
            try:
                cache_path.unlink()
            except OSError:
                pass

        self._consecutive_line_failures += 1
        n = self._consecutive_line_failures
        self._dbg.fail(
            f"第{chapter_num}章行{line_idx}",
            error_message,
            "已写入失败表并删除行缓存",
            f"连续失败{n}/{CONSECUTIVE_LINE_FAIL_PAUSE}",
            "断点续录将只重试失败行",
        )

        if n >= CONSECUTIVE_LINE_FAIL_PAUSE:
            self._halt_for_consecutive_failures = True
            with self.state._lock:
                self.state.paused = True
                self.state._pause_event.clear()
                self.state.status_message = (
                    f"已连续 {n} 行失败，全书录制已自动暂停"
                )
                self.state.last_error = (
                    f"连续 {n} 行录制失败，已自动暂停。"
                    "请检查 API/网络/风控后点击「继续」。"
                )
            self._dbg.fail(
                "全书录制",
                f"连续{n}行失败",
                "自动暂停",
                "已挂起线程",
                "处理 API/网络/台词后点「继续」",
            )

    def _run(
        self,
        api_key: str,
        novel_name: str,
        *,
        novel_fingerprint: str = "",
        resume: bool,
        chapter_nums: list[int] | None = None,
    ) -> None:
        try:
            ensure_ffmpeg_configured()
        except FFmpegNotAvailable as exc:
            raise StepAudioError(str(exc)) from exc

        folder = audiobook_output_dir(novel_name)
        if not chapter_nums:
            scope_label = "全书"
        elif len(chapter_nums) == 1:
            scope_label = f"第{chapter_nums[0]}章"
        else:
            scope_label = f"{len(chapter_nums)}章"
        self._dbg.ok(
            "录制",
            "开始",
            dir=folder.name,
            resume=str(resume),
            scope=scope_label,
        )
        prune_incomplete_chapter_mp3s(novel_name)

        with self._db_lock:
            with get_connection() as conn:
                all_chapters = list_script_chapters(conn)
                if novel_fingerprint:
                    self._pronunciation_rules = load_confirmed_rules_for_api(
                        conn, novel_fingerprint
                    )
                else:
                    self._pronunciation_rules = []
        if self._pronunciation_rules:
            self._dbg.line(
                "读音规则",
                "已加载",
                f"{len(self._pronunciation_rules)}条 confirmed",
            )

        if not all_chapters:
            raise StepAudioError("库内尚无剧本章节，请先完成剧本拆解。")

        if chapter_nums:
            wanted = sorted({int(n) for n in chapter_nums})
            chapters = [ch for ch in all_chapters if ch in wanted]
            missing = [n for n in wanted if n not in all_chapters]
            if missing:
                raise StepAudioError(f"库内未找到章节：{missing}")
            if not chapters:
                raise StepAudioError("未选择可录制的章节。")
        else:
            chapters = all_chapters

        total_ch = len(chapters)
        with self.state._lock:
            self.state.book_total = total_ch
            if chapter_nums:
                nums = ", ".join(str(n) for n in chapters[:8])
                if len(chapters) > 8:
                    nums += f" 等{len(chapters)}章"
                self.state.status_message = f"准备录制指定章节：{nums}…"
            else:
                self.state.status_message = "准备全书录制…"

        for book_idx, chapter_num in enumerate(chapters, 1):
            self.state.wait_if_paused()
            with self.state._lock:
                if self.state.reset_requested:
                    break
                self.state.book_current = book_idx
                self.state.chapter_current = chapter_num

            self._record_chapter(
                api_key,
                novel_name,
                chapter_num,
                book_idx=book_idx,
                book_total=total_ch,
                resume=resume,
            )

        with self.state._lock:
            if not self.state.reset_requested:
                self._set_recording_finish_status(
                    novel_name, chapter_nums=chapter_nums
                )
                self._dbg.ok("录制", "结束", scope=scope_label)

    def _set_recording_finish_status(
        self,
        novel_name: str,
        *,
        chapter_nums: list[int] | None,
    ) -> None:
        """根据库内实际进度设置结束态，避免「流程结束」与失败行/未完成章矛盾。"""
        from db import book_recording_progress_summary

        with self._db_lock:
            with get_connection() as conn:
                summary = book_recording_progress_summary(conn)

        lines_fail = int(summary.get("lines_failed") or 0)
        ch_done = int(summary.get("chapters_complete") or 0)
        ch_count = int(summary.get("chapter_count") or 0)
        lines_ok = int(summary.get("lines_ok") or 0)
        lines_total = int(summary.get("lines_total") or 0)
        scope = (
            f"指定 {len(chapter_nums)} 章"
            if chapter_nums
            else "全书"
        )

        with self.state._lock:
            self.state.running = False
            if lines_fail > 0:
                self.state.status_message = (
                    f"{scope}录制已停止：{lines_fail} 行失败，请断点续录。"
                )
                self.state.last_error = (
                    f"尚有 {lines_fail} 行未成功（成功 {lines_ok}/{lines_total}）。"
                    "失败行不会进入章 MP3；勾选断点续录后重试即可。"
                )
            elif ch_count > 0 and ch_done < ch_count:
                self.state.status_message = (
                    f"{scope}录制已停止：{ch_done}/{ch_count} 章已完成。"
                )
            elif chapter_nums:
                self.state.status_message = "指定章节录制完成。"
            else:
                self.state.status_message = "全书录制完成。"
                self.state.last_error = ""

    def _record_chapter(
        self,
        api_key: str,
        novel_name: str,
        chapter_num: int,
        *,
        book_idx: int,
        book_total: int,
        resume: bool,
    ) -> None:
        with self._db_lock:
            with get_connection() as conn:
                lines = fetch_script_lines_for_recording(conn, chapter_num)

        if not lines:
            self._dbg.skip(f"第{chapter_num}章", "无剧本行")
            return

        line_total = len(lines)
        with self.state._lock:
            self.state.chapter_total = line_total
            self.state.line_total = line_total

        existing_mp3 = resolve_chapter_mp3_path(novel_name, chapter_num)
        with self._db_lock:
            with get_connection() as conn:
                ok_n, failed_n, total_n = chapter_recording_progress(
                    conn, chapter_num
                )
        chapter_complete = total_n > 0 and ok_n >= total_n and failed_n == 0

        with self._db_lock:
            with get_connection() as conn:
                fully_ok, _, _ = chapter_lines_fully_recorded(
                    lines, novel_name, chapter_num
                )

        if resume and chapter_complete and fully_ok and existing_mp3.is_file():
            with self.state._lock:
                self.state.chapter_mp3_paths[chapter_num] = str(existing_mp3)
                if chapter_num not in self.state.completed_chapters:
                    self.state.completed_chapters.append(chapter_num)
            self._dbg.skip(f"第{chapter_num}章", "已完成")
            return

        if not resume:
            with self._db_lock:
                with get_connection() as conn:
                    clear_chapter_audio_tracking(conn, chapter_num)
                    conn.commit()
            cache_dir = chapter_cache_dir(novel_name, chapter_num)
            for p in cache_dir.glob("line_*.mp3"):
                try:
                    p.unlink()
                except OSError:
                    pass

        if failed_n > 0:
            self._dbg.line(f"第{chapter_num}章", "待重录", f"{failed_n}行失败")

        self._dbg.line(f"第{chapter_num}章", "开始", f"共{line_total}行")

        for line_idx, line in enumerate(lines, 1):
            if self._halt_for_consecutive_failures:
                break
            self.state.wait_if_paused()
            with self.state._lock:
                if self.state.reset_requested:
                    return
                self.state.line_current = line_idx
                self.state.status_message = (
                    f"全书 {book_idx}/{book_total} 章 · "
                    f"第 {chapter_num} 章 · 行 {line_idx}/{line_total}"
                )

            line_id = int(line["id"])
            role = str(line.get("role") or "").strip()
            content = str(line.get("content") or "").strip()
            instruction = str(line.get("emotion_instruction") or "").strip()
            is_dialogue = bool(line.get("is_dialogue"))
            cache_path = line_cache_audio_path(novel_name, chapter_num, line_id)

            if not _line_needs_recording(line, cache_path, resume=resume):
                self._register_line_success()
                continue

            prev_err = str(line.get("recording_error") or "").strip()
            if resume and str(line.get("recording_status") or "").strip() == RECORDING_STATUS_FAILED:
                with self._db_lock:
                    with get_connection() as conn:
                        prepare_failed_line_for_step_retry(conn, line_id)
                        conn.commit()
                if cache_path.is_file():
                    try:
                        cache_path.unlink()
                    except OSError:
                        pass
                self._dbg.line(
                    f"第{chapter_num}章行{line_idx}",
                    "续录",
                    "先重试Step整句/切片"
                    + (f" 上次:{prev_err[:80]}" if prev_err else ""),
                )

            with self._db_lock:
                with get_connection() as conn:
                    voice_id, profile, voice_source = resolve_voice_for_script_role(
                        conn,
                        role,
                        voice_id_from_row=str(line.get("voice_id") or ""),
                    )

            preview = content[:24] + ("…" if len(content) > 24 else "")
            self._dbg.line(
                f"第{chapter_num}章行{line_idx}",
                "合成",
                f"role={role} {preview}"
                + (f" [{voice_source}]" if voice_source not in ("exact_cast", "script_line") else ""),
            )

            if not voice_id:
                self._register_line_failure(
                    chapter_num=chapter_num,
                    line_idx=line_idx,
                    line_id=line_id,
                    error_message=f"角色「{role}」未绑定音色",
                    cache_path=cache_path,
                )
                if self._halt_for_consecutive_failures:
                    break
                continue

            try:
                pronunciation_tone = tone_rules_for_spoken_text(
                    self._pronunciation_rules,
                    content,
                )
                result = synthesize_line_audio_with_retry(
                    api_key,
                    content=content,
                    voice_id=voice_id,
                    instruction=instruction,
                    age=profile.get("age", ""),
                    gender=profile.get("gender", ""),
                    log_debug=self._dbg,
                    pause_check=self.state.wait_if_paused,
                    pronunciation_tone=pronunciation_tone or None,
                )
            except RecordingPaused:
                raise
            except LineRecordingFailed as exc:
                self._register_line_failure(
                    chapter_num=chapter_num,
                    line_idx=line_idx,
                    line_id=line_id,
                    error_message=str(exc),
                    cache_path=cache_path,
                )
                if self._halt_for_consecutive_failures:
                    break
                continue

            save_line_cache(novel_name, chapter_num, line_id, result.audio_bytes)
            self._register_line_success()
            duration = audio_duration_seconds(result.audio_bytes)
            self._dbg.ok(
                f"第{chapter_num}章行{line_idx}",
                f"缓存{duration:.1f}s",
                engine=result.engine,
                voice=result.actual_voice_id,
            )
            gap = gap_seconds_after_line(line)

            with self._db_lock:
                with get_connection() as conn:
                    update_script_line_audio_tracking(
                        conn,
                        line_id,
                        actual_voice_id=result.actual_voice_id,
                        audio_duration=duration,
                        gap_duration=gap,
                        start_time_offset=0.0,
                        end_time_offset=0.0,
                    )
                    conn.commit()

        with self._db_lock:
            with get_connection() as conn:
                lines = fetch_script_lines_for_recording(conn, chapter_num)
                out_path, seg_n, failed_n = reassemble_chapter_from_line_caches(
                    novel_name,
                    chapter_num,
                    lines,
                    conn,
                    log=lambda msg: self._dbg.line(
                        "章合拢", "OK" if "exported" in msg else "INFO", msg
                    ),
                )
                conn.commit()

        if out_path is not None:
            with self.state._lock:
                self.state.chapter_mp3_paths[chapter_num] = str(out_path)
                if chapter_num not in self.state.completed_chapters:
                    self.state.completed_chapters.append(chapter_num)
            self._dbg.ok(
                f"第{chapter_num}章",
                "导出MP3",
                file=out_path.name,
                lines=str(seg_n),
            )
        elif failed_n > 0:
            self._dbg.line(
                f"第{chapter_num}章",
                "未导出",
                f"{failed_n}行失败 续录后自动合拢",
            )
        else:
            ok_n, _, total_n = 0, 0, len(lines)
            with self._db_lock:
                with get_connection() as conn:
                    ok_n, _, total_n = chapter_recording_progress(conn, chapter_num)
            if ok_n < total_n:
                self._dbg.line(
                    f"第{chapter_num}章",
                    "未导出",
                    f"仅{ok_n}/{total_n}行完成",
                )

        if self._halt_for_consecutive_failures:
            self._dbg.line("全书录制", "等待", "连续失败后已暂停 请点击继续")
            self.state.wait_if_paused()
            if self.state.reset_requested:
                return
            with self._db_lock:
                with get_connection() as conn:
                    _, fail_n, _ = chapter_recording_progress(conn, chapter_num)
            if fail_n > 0 and not self._halt_for_consecutive_failures:
                self._record_chapter(
                    api_key,
                    novel_name,
                    chapter_num,
                    book_idx=book_idx,
                    book_total=book_total,
                    resume=True,
                )
