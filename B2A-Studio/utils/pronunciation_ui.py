"""读音校正 UI：本地扫描、确认、试听、导入导出。"""

from __future__ import annotations

import streamlit as st

from db import (
    PRONUNCIATION_STATUS_CONFIRMED,
    PRONUNCIATION_STATUS_IGNORED,
    PRONUNCIATION_STATUS_PENDING,
    delete_pronunciation_rule,
    get_connection,
    list_pronunciation_rules,
    pronunciation_rules_summary,
    upsert_pronunciation_rule,
)
from utils.pronunciation import (
    _STEP_TONE_OFFICIAL_EXAMPLES,
    default_tone_rule,
    export_rules_json,
    filter_rules_for_display,
    import_rules_json,
    is_valid_step_tone_rule,
    merge_scan_into_pending,
    prune_substring_rules_on_confirm,
    purge_pending_rules_not_matching,
    scan_pronunciation_candidates,
    tone_rules_for_spoken_text,
)
from utils.step_audio import StepAudioError, synthesize_casting_preview


def _api_key() -> str:
    return (st.session_state.get("step_api_key") or "").strip()


def render_pronunciation_panel(*, novel_fingerprint: str) -> None:
    fp = (novel_fingerprint or "").strip()
    if not fp:
        st.caption("上传小说后可管理本书读音规则。")
        return

    st.caption(
        "扫描 **剧本正文**（零 LLM token），列出 **机构/专名** 中含易读错多音字"
        f"（如「调」）且出现 ≥ 最低次数的词组；确认后写入 "
        "`extra_body.pronunciation_map.tone`（与 Step 官方示例一致）。"
    )
    st.caption(
        "官方格式示例："
        + " · ".join(f"`{ex}`" for ex in _STEP_TONE_OFFICIAL_EXAMPLES)
        + "。**勿用**整词全拼如 `郭长城/guo1 chang2 cheng2`（会念出数字）。"
        "**勿添加演员人名。**"
    )

    with get_connection() as conn:
        summary = pronunciation_rules_summary(conn, fp)

    m1, m2, m3 = st.columns(3)
    m1.metric("待确认", summary.get("pending", 0))
    m2.metric("已确认", summary.get("confirmed", 0))
    m3.metric("已忽略", summary.get("ignored", 0))

    tool1, tool2, tool3, tool4 = st.columns([1, 1, 1.2, 1.2])
    with tool1:
        min_freq = st.number_input(
            "最低出现次数",
            min_value=1,
            max_value=20,
            value=2,
            step=1,
            key="pron_min_freq",
            help="扫描与列表筛选共用：仅含多音字且出现次数 ≥ 此值的待确认项会显示/入库",
        )
    with tool2:
        if st.button("🔍 扫描剧本", key="pron_scan_btn", use_container_width=True):
            with get_connection() as conn:
                purged = purge_pending_rules_not_matching(
                    conn, fp, min_freq=int(min_freq)
                )
                candidates = scan_pronunciation_candidates(
                    conn,
                    min_freq_oov=int(min_freq),
                )
                added = merge_scan_into_pending(conn, fp, candidates)
                conn.commit()
            msg = f"扫描完成：候选 {len(candidates)} 条，新入库 {added} 条。"
            if purged:
                msg += f" 已清理 {purged} 条不符合条件的待确认项。"
            st.success(msg)
            st.rerun()
    with tool3:
        if st.button("🧹 清理待确认", key="pron_purge_btn", use_container_width=True):
            with get_connection() as conn:
                purged = purge_pending_rules_not_matching(
                    conn, fp, min_freq=int(min_freq)
                )
                conn.commit()
            st.success(f"已清理 {purged} 条待确认（无多音字或次数不足）。")
            st.rerun()
    with tool4:
        show_ignored = st.checkbox("显示已忽略", key="pron_show_ignored")

    # 外层 app.py 已用 expander 折叠「读音校正」；此处不可再嵌套 expander
    if st.checkbox("➕ 手动添加 / 导入导出", key="pron_show_manual", value=False):
        man1, man2, man3 = st.columns([1.2, 1.5, 1])
        with man1:
            manual_src = st.text_input("词组", key="pron_manual_src", placeholder="特调处")
        with man2:
            manual_tone = st.text_input(
                "tone 规则",
                key="pron_manual_tone",
                placeholder="特调处/特diao4处",
            )
        with man3:
            st.write("")
            if st.button("添加", key="pron_manual_add"):
                src = (manual_src or "").strip()
                if not src:
                    st.error("请填写词组。")
                elif not (manual_tone or "").strip() and not default_tone_rule(src):
                    st.error("无法自动生成规则；请手动填写官方格式，如 特调处/特diao4处")
                else:
                    tone = (manual_tone or "").strip() or default_tone_rule(src)
                    if not is_valid_step_tone_rule(tone):
                        st.error(
                            "tone 规则格式不符 Step 官方要求。"
                            "请用「词组/混合注音」如 阿胶/e1胶、特调处/特diao4处，"
                            "勿用空格分隔的整词全拼。"
                        )
                    else:
                        with get_connection() as conn:
                            upsert_pronunciation_rule(
                                conn,
                                novel_fingerprint=fp,
                                source_text=src,
                                tone_rule=tone,
                                status=PRONUNCIATION_STATUS_CONFIRMED,
                            )
                            prune_substring_rules_on_confirm(conn, fp, src)
                            conn.commit()
                        st.success(f"已添加：{src}")
                        st.rerun()

        with get_connection() as conn:
            rules_export = list_pronunciation_rules(conn, fp)
        exp_col, imp_col = st.columns(2)
        with exp_col:
            st.download_button(
                "⬇️ 导出已确认 JSON",
                data=export_rules_json(rules_export).encode("utf-8"),
                file_name="读音规则.json",
                mime="application/json",
                key="pron_export_json",
            )
        with imp_col:
            uploaded = st.file_uploader(
                "导入 JSON（已确认规则）",
                type=["json"],
                key="pron_import_json",
            )
            if uploaded and st.button("执行导入", key="pron_import_btn"):
                try:
                    raw = uploaded.getvalue().decode("utf-8")
                    with get_connection() as conn:
                        n = import_rules_json(conn, fp, raw)
                        conn.commit()
                    st.success(f"已导入 {n} 条规则。")
                    st.rerun()
                except Exception as exc:
                    st.error(f"导入失败：{exc}")

    with get_connection() as conn:
        all_rows = list_pronunciation_rules(conn, fp)
        rows = filter_rules_for_display(
            all_rows,
            min_freq=int(min_freq),
            include_ignored=show_ignored,
        )

    if not rows:
        pending_total = summary.get("pending", 0)
        if pending_total > 0:
            st.info(
                f"库内有 {pending_total} 条待确认，但无符合当前筛选"
                f"（≥{min_freq} 次且含多音字）的项。可调低次数或点「清理待确认」。"
            )
        else:
            st.info("尚无读音规则。点击「扫描剧本」或手动添加。")
        return

    hidden_pending = sum(
        1
        for r in all_rows
        if (r.get("status") or "") == PRONUNCIATION_STATUS_PENDING
    ) - sum(1 for r in rows if (r.get("status") or "") == PRONUNCIATION_STATUS_PENDING)
    list_hint = f"显示 **{len(rows)}** 条"
    if hidden_pending > 0:
        list_hint += f"（另有 {hidden_pending} 条待确认未达筛选条件，已隐藏）"
    st.markdown(f"**规则列表** · {list_hint}")
    narrator_voice = _default_preview_voice()

    for row in rows:
        rid = int(row["id"])
        src = str(row.get("source_text") or "")
        status = str(row.get("status") or PRONUNCIATION_STATUS_PENDING)
        hit = int(row.get("hit_count") or 0)
        ctx = str(row.get("context_sample") or "")
        tone_key = f"pron_tone_{rid}"
        if tone_key not in st.session_state:
            st.session_state[tone_key] = str(row.get("tone_rule") or default_tone_rule(src))

        status_label = {
            PRONUNCIATION_STATUS_PENDING: "待确认",
            PRONUNCIATION_STATUS_CONFIRMED: "已确认",
            PRONUNCIATION_STATUS_IGNORED: "已忽略",
        }.get(status, status)

        with st.container(border=True):
            h1, h2, h3 = st.columns([1.2, 0.6, 1.2])
            with h1:
                st.markdown(f"**{src}** · {hit} 次 · _{status_label}_")
            with h2:
                st.caption(ctx[:80] + ("…" if len(ctx) > 80 else "") if ctx else "—")
            with h3:
                tone_val = st.text_input(
                    "tone 规则",
                    key=tone_key,
                    label_visibility="collapsed",
                )

            b1, b2, b3, b4, b5 = st.columns(5)
            with b1:
                if st.button("✓ 确认", key=f"pron_ok_{rid}"):
                    tone = (st.session_state.get(tone_key) or "").strip()
                    if not is_valid_step_tone_rule(tone):
                        st.error(
                            "tone 规则须为官方格式，如 特调处/特diao4处、阿胶/e1胶；"
                            "勿用整词全拼（含空格音节）。"
                        )
                    else:
                        with get_connection() as conn:
                            upsert_pronunciation_rule(
                                conn,
                                novel_fingerprint=fp,
                                source_text=src,
                                tone_rule=tone,
                                status=PRONUNCIATION_STATUS_CONFIRMED,
                                hit_count=hit,
                                context_sample=ctx,
                            )
                            prune_substring_rules_on_confirm(conn, fp, src)
                            conn.commit()
                        st.rerun()
            with b2:
                if st.button("忽略", key=f"pron_skip_{rid}"):
                    with get_connection() as conn:
                        upsert_pronunciation_rule(
                            conn,
                            novel_fingerprint=fp,
                            source_text=src,
                            tone_rule=st.session_state.get(tone_key, ""),
                            status=PRONUNCIATION_STATUS_IGNORED,
                            hit_count=hit,
                            context_sample=ctx,
                        )
                        conn.commit()
                    st.rerun()
            with b3:
                if st.button("🎵 试听", key=f"pron_prev_{rid}"):
                    api_key = _api_key()
                    if not api_key:
                        st.error("请先在侧边栏配置 Step API Key。")
                    else:
                        sample = ctx if src in ctx else f"……{src}……"
                        tone_list = tone_rules_for_spoken_text(
                            [{"source_text": src, "tone_rule": st.session_state.get(tone_key, "")}],
                            sample,
                        )
                        try:
                            with st.spinner("试听合成中…"):
                                audio = synthesize_casting_preview(
                                    api_key,
                                    voice_id=narrator_voice,
                                    quote_text=sample[:200],
                                    emotion_instruction="平缓旁白",
                                    pronunciation_tone=tone_list or None,
                                )
                            st.audio(audio, format="audio/mp3")
                        except StepAudioError as exc:
                            st.error(str(exc))
            with b4:
                if st.button("保存 tone", key=f"pron_save_{rid}"):
                    with get_connection() as conn:
                        upsert_pronunciation_rule(
                            conn,
                            novel_fingerprint=fp,
                            source_text=src,
                            tone_rule=st.session_state.get(tone_key, ""),
                            status=status,
                            hit_count=hit,
                            context_sample=ctx,
                        )
                        conn.commit()
                    st.success("已保存。")
            with b5:
                if st.button("删除", key=f"pron_del_{rid}"):
                    with get_connection() as conn:
                        delete_pronunciation_rule(conn, rid)
                        conn.commit()
                    st.rerun()


def _default_preview_voice() -> str:
    voices = st.session_state.get("system_voices") or []
    if voices:
        first = voices[0]
        if isinstance(first, dict):
            return str(first.get("voice_id") or first.get("id") or "ruyananshi")
        return str(getattr(first, "voice_id", None) or "ruyananshi")
    return "ruyananshi"


def render_pronunciation_recording_hint(*, novel_fingerprint: str) -> None:
    fp = (novel_fingerprint or "").strip()
    if not fp:
        return
    with get_connection() as conn:
        summary = pronunciation_rules_summary(conn, fp)
    pending = int(summary.get("pending", 0))
    if pending > 0:
        st.warning(
            f"尚有 **{pending}** 条读音规则待确认。"
            "建议先在「读音校正」中确认后再录制，以免 StepAudio 误读生僻专名。"
        )
