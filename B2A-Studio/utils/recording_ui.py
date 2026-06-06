"""有声书录音棚 UI。"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

import streamlit as st

from .audiobook_ffmpeg import FFmpegNotAvailable, ensure_ffmpeg_configured
from .audiobook_synth import check_edge_tts_version
from .audiobook_assembly import (
    quick_recording_library_sync,
    scan_duration_anomalies,
)
from .audiobook_paths import (
    audiobook_output_dir,
    resolve_chapter_mp3_path,
    sanitize_novel_title,
)
from .audiobook_recorder import AudiobookRecorder
from db import (
    book_recording_progress_summary,
    chapter_recording_progress,
    ensure_database,
    fetch_failed_script_lines,
    get_connection,
    get_pipeline_stats,
    list_script_chapters,
)
from .recording_log import LOG_FILE, read_recording_log_tail, snapshot_recording_log
from .ui_scroll import ANCHOR_RECORDING, request_scroll_to

_RECORDER_KEY = "audiobook_recorder"
# 录制中 UI 刷新间隔（秒）。过短会触发 WebSocket 风暴导致浏览器 tab 崩溃。
_RECORDING_LIVE_TICK_SEC = 4.0
_RECORDING_LIVE_ACTIVE_KEY = "_recording_live_active"


def _get_recorder() -> AudiobookRecorder:
    if _RECORDER_KEY not in st.session_state:
        st.session_state[_RECORDER_KEY] = AudiobookRecorder()
    return st.session_state[_RECORDER_KEY]


def _init_recording_session() -> None:
    defaults = {
        "recording_log_snapshot": [],
        "recording_preview_paths": {},
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def render_audiobook_recording_studio() -> None:
    """批量录制中心：控制面板、双进度条、折叠日志、章节试听舱。"""
    _init_recording_session()
    ensure_database()
    novel_name = st.session_state.get("uploaded_novel_name", "")
    novel_text = st.session_state.get("uploaded_novel_text", "")
    api_key = (st.session_state.get("step_api_key") or "").strip()

    if not novel_text:
        st.info("请先上传小说并完成剧本拆解、试镜绑定音色后，再进入录制。")
        return

    with get_connection() as conn:
        stats = get_pipeline_stats(conn)
        chapters = list_script_chapters(conn)

    if stats["script_lines"] == 0:
        st.warning("库内尚无剧本行，请先完成上方「剧本智能拆解」或导入离线剧本 CSV。")
        return

    # 轻量同步（秒级）；勿在加载时合拢全部已完成章节（会阻塞整页数分钟）
    _sync_key = f"recording_lib_sync_v2::{novel_name}"
    if not st.session_state.get(_sync_key):
        with st.spinner("正在同步录制库（校验标点失败行与章 MP3）…"):
            fixed, removed, reassembled, duration_queued = (
                quick_recording_library_sync(novel_name)
            )
        st.session_state[_sync_key] = True
        if fixed or removed or reassembled or duration_queued:
            parts: list[str] = []
            if fixed:
                parts.append(f"修复标点失败行 {fixed} 条")
            if removed:
                parts.append(f"移除未完成章 MP3 {removed} 个")
            if reassembled:
                parts.append(f"自动合拢已完成章 MP3 {reassembled} 个")
            if duration_queued:
                parts.append(
                    f"时长异常行 {duration_queued} 条已标记待续录（无需整章重录）"
                )
            st.caption("同步完成：" + "，".join(parts) + "。")

    out_dir = audiobook_output_dir(novel_name)
    st.caption(
        "主引擎：**StepAudio 2.5**（走 StepPlan 额度）· "
        "应急兜底：**Edge-TTS**（微软朗读接口，不收费；Step 审核拦截或 "
        "整句时长异常且等待重试仍失败时启用）。"
    )
    st.caption(f"成品输出目录：`{out_dir}`（位于 `_local/`，已 gitignore，不会提交到 GitHub）")

    try:
        ensure_ffmpeg_configured()
    except FFmpegNotAvailable as exc:
        st.error(str(exc))
        return

    edge_ok, edge_ver = check_edge_tts_version()
    if not edge_ok:
        st.warning(
            f"edge-tts {edge_ver} 过旧，请执行 "
            "pip install \"edge-tts>=7.2.7\" 后重启 Streamlit。"
        )

    with get_connection() as conn:
        db_progress = book_recording_progress_summary(conn)
        chapter_labels: dict[int, str] = {}
        incomplete_chapters: list[int] = []
        for ch in chapters:
            ok_n, failed_n, total_n = chapter_recording_progress(conn, ch)
            if total_n > 0 and ok_n >= total_n and failed_n == 0:
                tag = "已完成"
            elif failed_n > 0:
                tag = f"{ok_n}/{total_n} 行·{failed_n}失败"
            elif ok_n > 0:
                tag = f"{ok_n}/{total_n} 行"
            else:
                tag = f"未录 {total_n} 行"
            chapter_labels[ch] = f"第 {ch} 章（{tag}）"
            if total_n > 0 and (ok_n < total_n or failed_n > 0):
                incomplete_chapters.append(ch)

    recorder = _get_recorder()
    state = recorder.state

    if "recording_preview_paths" not in st.session_state:
        st.session_state.recording_preview_paths = {}

    record_scope = st.radio(
        "录制范围",
        ["全书", "指定章节"],
        horizontal=True,
        disabled=state.running,
    )
    selected_chapter_nums: list[int] | None = None
    if record_scope == "指定章节":
        default_pick = incomplete_chapters or chapters
        picked = st.multiselect(
            "选择要录制的章节（可多选）",
            options=chapters,
            default=default_pick,
            format_func=lambda ch: chapter_labels.get(ch, f"第 {ch} 章"),
            disabled=state.running,
        )
        selected_chapter_nums = [int(ch) for ch in picked]
        if picked:
            st.caption(
                f"已选 **{len(picked)}** 章："
                + "、".join(f"第 {ch} 章" for ch in sorted(picked)[:12])
                + ("…" if len(picked) > 12 else "")
            )
        else:
            st.caption("请至少选择一章。")

    start_label = (
        "▶️ 开始录制所选章节"
        if record_scope == "指定章节"
        else "▶️ 开始全书录制"
    )

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        start_clicked = st.button(
            start_label,
            type="primary",
            use_container_width=True,
            disabled=not api_key or state.running,
        )
    with c2:
        pause_clicked = st.button(
            "⏸️ 暂停",
            use_container_width=True,
            disabled=not state.running or state.paused,
        )
    with c3:
        resume_clicked = st.button(
            "▶️ 继续",
            use_container_width=True,
            disabled=not state.running or not state.paused,
        )
    with c4:
        reset_clicked = st.button(
            "🔄 重置",
            use_container_width=True,
        )

    resume_from_checkpoint = st.checkbox(
        "断点续录（仅重试失败/未完成行，成功后自动重算该章时间轴并合拢）",
        value=True,
        help="关闭后将清空各章缓存与时间轴后全量重录。单行失败不会插入静音。",
    )

    if start_clicked:
        if not api_key:
            st.error(
                "请先在侧边栏配置 Step API Key，并点击保存（写入 .env）后再录制。"
            )
        elif not chapters:
            st.error("没有可录制的章节。")
        elif record_scope == "指定章节" and not selected_chapter_nums:
            st.error("请先选择至少一个章节。")
        else:
            st.session_state.recording_log_snapshot = []
            request_scroll_to(ANCHOR_RECORDING)
            recorder.start(
                api_key=api_key,
                novel_name=novel_name,
                resume=resume_from_checkpoint,
                chapter_nums=(
                    selected_chapter_nums
                    if record_scope == "指定章节"
                    else None
                ),
            )
            st.rerun()

    if pause_clicked:
        request_scroll_to(ANCHOR_RECORDING)
        recorder.pause()
        st.rerun()
    if resume_clicked:
        request_scroll_to(ANCHOR_RECORDING)
        recorder.resume()
        st.rerun()
    if reset_clicked:
        request_scroll_to(ANCHOR_RECORDING)
        recorder.reset()
        st.session_state[_RECORDER_KEY] = AudiobookRecorder()
        st.session_state.recording_preview_paths = {}
        st.session_state.recording_log_snapshot = []
        st.rerun()

    finished_msg = (state.status_message or "").strip()
    book_done = not state.running and "结束" in finished_msg
    partial_scope = bool(state.chapter_filter)

    if state.running:
        st.info(
            "🔴 **录制进行中** — 下方进度每 "
            f"{_RECORDING_LIVE_TICK_SEC:.0f} 秒自动更新。"
            "折叠区与试听列表在录制结束后恢复，**请勿频繁刷新页面**。"
        )
        _install_recording_live_tick()
        return

    if book_done:
        book_ratio = 1.0
        line_ratio = 1.0
        if partial_scope:
            book_caption = "所选章节录制已完成"
        else:
            book_caption = "全书录制已完成"
        line_caption = "各章 MP3 已写入下方成品输出目录"
    else:
        ch_count = int(db_progress["chapter_count"] or 0)
        ch_done = int(db_progress["chapters_complete"] or 0)
        active_ch = db_progress["active_chapter"]
        active_ok = int(db_progress["active_ok"] or 0)
        active_fail = int(db_progress["active_failed"] or 0)
        active_total = int(db_progress["active_total"] or 0)
        lines_ok = int(db_progress["lines_ok"] or 0)
        lines_fail = int(db_progress["lines_failed"] or 0)
        lines_total = int(db_progress["lines_total"] or 0)

        if ch_count and ch_done < ch_count and active_total > 0:
            book_ratio = min(
                1.0,
                (ch_done + active_ok / active_total) / ch_count,
            )
        elif ch_count:
            book_ratio = min(1.0, ch_done / ch_count)
        else:
            book_ratio = 0.0

        line_ratio = (
            min(1.0, active_ok / active_total) if active_total else 0.0
        )
        book_caption = (
            f"已从数据库恢复 · **{ch_done}/{ch_count}** 章全部完成"
            f" · 累计 **{lines_ok}/{lines_total}** 行成功"
        )
        if lines_fail:
            book_caption += f" · **{lines_fail}** 行失败待续录"
        if active_ch is not None and active_total:
            line_caption = (
                f"当前关注 **第 {active_ch} 章** · "
                f"**{active_ok}/{active_total}** 行成功"
            )
            if active_fail:
                line_caption += f" · **{active_fail}** 行失败"
        else:
            line_caption = "尚无行级录制记录"

    st.markdown("**全书进度（按章）**")
    st.progress(book_ratio)
    st.caption(book_caption)
    st.markdown("**当前章进度（按行）**")
    st.progress(line_ratio)
    st.caption(line_caption)

    status = state.status_message or (
        "运行中…" if state.running else "待命"
    )
    if state.paused:
        status = "⏸️ 已暂停 · " + status
    elif state.running:
        status = "🔴 录制中 · " + status
    elif book_done:
        status = (
            f"✅ 录制完成 · {finished_msg} "
            f"请到成品输出目录查收：`{out_dir}`"
        )
    st.markdown(f"**状态**：{status}")
    if state.last_error:
        st.error(state.last_error)

    preview_map: dict[int, str] = {}
    for ch in chapters:
        mp3 = resolve_chapter_mp3_path(novel_name, ch)
        if mp3.is_file():
            preview_map[ch] = str(mp3)
    for ch, path in list(state.chapter_mp3_paths.items()):
        if path and Path(path).is_file():
            preview_map[ch] = path
    st.session_state.recording_preview_paths = preview_map

    st.markdown(
        '<div class="b2a-recording-panels-marker" aria-hidden="true"></div>',
        unsafe_allow_html=True,
    )

    with st.expander("⏱ 时长异常行扫描", expanded=False):
        st.caption(
            "检测 Step 返回过长音频（如 4 字对白 30 秒）。可标记待续录："
            "**只重跑异常行**，断点续录时会自动跳过其余已成功行，章末再合拢 MP3。"
        )
        if st.button("扫描全书已录行", key="scan_duration_anomalies"):
            with st.spinner("扫描中…"):
                anomalies = scan_duration_anomalies(novel_name)
            st.session_state["duration_anomaly_scan"] = anomalies
        anomalies = st.session_state.get("duration_anomaly_scan") or []
        if anomalies:
            st.warning(f"发现 **{len(anomalies)}** 行时长异常。")
            for row in anomalies[:40]:
                st.markdown(
                    f"- 第 **{row['chapter_num']}** 章 · 行 **{row['line_idx']}** · "
                    f"{row.get('role') or '—'} · 「{row.get('content') or ''}」 · "
                    f"**{row['duration_sec']}s**（预期 ≤{row['expected_max_sec']}s）"
                )
            if len(anomalies) > 40:
                st.caption(f"… 另有 {len(anomalies) - 40} 行未列出")
            if st.button("标记以上行待续录重跑", key="queue_duration_anomalies"):
                from utils.audiobook_assembly import queue_lines_for_rerecord

                n = queue_lines_for_rerecord(
                    novel_name,
                    [int(r["line_id"]) for r in anomalies],
                )
                st.success(f"已标记 {n} 行；请使用「断点续录」重跑，无需整章重录。")
                st.session_state["duration_anomaly_scan"] = []
        elif st.session_state.get("duration_anomaly_scan") is not None:
            st.success("未发现时长异常行。")

    with get_connection() as conn:
        failed_rows = fetch_failed_script_lines(conn, limit=30)
    if failed_rows:
        with st.expander(
            f"⚠️ 单行录制失败记录（{len(failed_rows)} 条，续录将自动重试）",
            expanded=True,
        ):
            st.caption(
                "失败行不会进入章 MP3；补录成功后整章时间轴与成品将自动重新合拢。"
            )
            for row in failed_rows:
                st.markdown(
                    f"- **第 {row['chapter_num']} 章 · 行 {row['line_idx']}** · "
                    f"{row.get('role') or '—'} · "
                    f"「{row.get('content_preview') or ''}」  \n"
                    f"  _{row.get('recording_error') or ''}_"
                )

    with st.expander(
        "🛠 调试日志（仅排查问题时展开）",
        expanded=False,
    ):
        log_body = (
            state.snapshot_logs()
            if state.running
            else snapshot_recording_log(max_lines=120)
        )
        if not log_body:
            log_body = read_recording_log_tail(max_lines=120)
        if log_body:
            st.caption(
                f"`{LOG_FILE}` · 每行一条 · `时:分:秒 | 动作 | 结果 | 详情`"
            )
            log_text = "\n".join(log_body[-120:])
            st.text_area(
                "recording_debug_log",
                value=log_text,
                height=280,
                disabled=True,
                label_visibility="collapsed",
            )
        else:
            st.caption("暂无调试记录；进度请以上方进度条为准。")

    preview_count = len(preview_map)
    with st.expander(
        "✨ 录制完成章节试听",
        expanded=False,
    ):
        st.caption(f"共 **{preview_count}** 章可试听")
        if not preview_map:
            st.caption(
                "某一章 **全部行** 录制成功后会自动合拢该章 MP3 并在此试听；"
                "打开本页时也会补全已有完成章的缺失成品。"
            )
        else:
            for ch in sorted(preview_map):
                mp3_path = preview_map[ch]
                p = Path(mp3_path)
                if not p.is_file():
                    continue
                st.markdown(f"**第 {ch} 章** · `{p.name}`")
                try:
                    st.audio(str(p))
                except Exception as exc:
                    st.warning(f"无法加载音频：{exc}")


def _render_live_recording_progress(
    state,
    *,
    partial_scope: bool,
) -> None:
    """录制中专用：只读内存 RecordingState，不查库。"""
    book_total = state.book_total or 1
    book_ratio = min(1.0, state.book_current / book_total) if book_total else 0.0
    line_total = state.line_total or 1
    line_ratio = min(1.0, state.line_current / line_total) if line_total else 0.0
    scope_word = "所选" if partial_scope else "全书"
    book_caption = (
        f"录制中 · {scope_word}第 {state.book_current}/{book_total} 章"
        f"（第 {state.chapter_current} 章）"
    )
    line_caption = (
        f"录制中 · 第 {state.chapter_current} 章"
        f" · 行 {state.line_current}/{line_total}"
    )
    status = state.status_message or "运行中…"
    if state.paused:
        status = "⏸️ 已暂停 · " + status
    else:
        status = "🔴 录制中 · " + status

    st.markdown("**全书进度（按章）**")
    st.progress(book_ratio)
    st.caption(book_caption)
    st.markdown("**当前章进度（按行）**")
    st.progress(line_ratio)
    st.caption(line_caption)
    st.markdown(f"**状态**：{status}")
    if state.last_error:
        st.error(state.last_error)

    logs = state.snapshot_logs()
    if logs:
        st.caption(f"最新 · `{logs[-1][:160]}`")


def _recording_live_fragment_body() -> None:
    """Fragment 定时回调：仅刷新进度区，禁止 st.rerun()（会触发全页重跑）。"""
    recorder = _get_recorder()
    state = recorder.state
    if not state.running:
        if st.session_state.pop(_RECORDING_LIVE_ACTIVE_KEY, False):
            st.rerun()
        return
    st.session_state[_RECORDING_LIVE_ACTIVE_KEY] = True
    _render_live_recording_progress(
        state,
        partial_scope=bool(state.chapter_filter),
    )


if hasattr(st, "fragment"):
    _recording_live_fragment = st.fragment(
        run_every=timedelta(seconds=_RECORDING_LIVE_TICK_SEC),
    )(_recording_live_fragment_body)
else:
    _recording_live_fragment = None


def _install_recording_live_tick() -> None:
    """录制中定时刷新进度 fragment；不触发整页 rerun。"""
    if _recording_live_fragment is not None:
        _recording_live_fragment()
        return
    _recording_live_fragment_body()
    time.sleep(_RECORDING_LIVE_TICK_SEC)
    if _get_recorder().state.running:
        st.rerun()
