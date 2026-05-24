"""铸模导演墙（Casting Room）Streamlit 界面。"""

from __future__ import annotations

import html

import streamlit as st

from .casting_backup import export_casting_backup, import_casting_backup, list_casting_backups
from db import (
    ROLLING_RANK_TOP_N,
    bind_character_voice,
    fetch_main_cast_characters,
    get_connection,
    get_pipeline_stats,
    list_character_voice_assignments,
)
from .extra_stock import fetch_stock_extra_characters, normalize_gender
from .step_audio import (
    BUNDLED_VOICES_CATALOG_REV,
    StepAudioError,
    SystemVoice,
    bundled_system_voices,
    synthesize_casting_preview,
    voice_select_options,
)


def ensure_bundled_voices_in_session(*, force: bool = False) -> None:
    """加载内置 StepAudio 2.5 官方音色清单到 session（与平台文档一致，不请求开放平台）。"""
    rev_ok = (
        st.session_state.get("system_voices_catalog_rev") == BUNDLED_VOICES_CATALOG_REV
    )
    if st.session_state.get("system_voices") and rev_ok and not force:
        return
    voices = bundled_system_voices()
    st.session_state.system_voices = voices
    st.session_state.system_voices_catalog_rev = BUNDLED_VOICES_CATALOG_REV
    st.session_state.system_voices_error = (
        "" if voices else "内置音色库文件缺失或为空，请检查 data/step_tts2_system_voices.json。"
    )


def _cast_tone_class(character: dict, *, is_stock: bool = False) -> str:
    """折叠/展开卡片共用：旁白灰 · 男主蓝 · 女主红 · 龙套浅蓝/浅红。"""
    if character.get("is_narrator"):
        return "tone-narrator"
    stock = is_stock or bool(character.get("is_stock_extra"))
    gender = normalize_gender(str(character.get("gender") or ""))
    if stock:
        return "tone-female-stock" if gender == "女" else "tone-male-stock"
    if gender == "女":
        return "tone-female-main"
    if gender == "男":
        return "tone-male-main"
    return "tone-narrator"


def _cast_card_html(character: dict) -> str:
    name = html.escape(str(character.get("name") or ""))
    gender = html.escape(str(character.get("gender") or "—"))
    age = html.escape(str(character.get("age") or "—"))
    personality = html.escape(
        str(character.get("personality") or "（暂无人设侧写）")
    )
    q1 = html.escape(str(character.get("quote_1") or "").strip() or "—")
    q2 = html.escape(str(character.get("quote_2") or "").strip() or "—")
    q1i = html.escape(str(character.get("quote_1_instruction") or "—"))
    q2i = html.escape(str(character.get("quote_2_instruction") or "—"))
    line_n = int(character.get("dialogue_lines") or 0)
    if character.get("is_narrator"):
        line_label = f"旁白 {line_n} 行"
    elif character.get("is_stock_extra"):
        line_label = f"匹配配角 {line_n} 人"
    else:
        line_label = f"对白 {line_n} 句"
    tone = _cast_tone_class(character)
    d, sp = "div", "span"
    return (
        f'<{d} class="b2a-cast-card {tone}">'
        f'<{d} class="b2a-cast-card-head">'
        f'<{sp} class="b2a-cast-name">{name}</{sp}>'
        f'<{sp} class="b2a-cast-meta">{gender} · {age} · {line_label}</{sp}>'
        f"</{d}>"
        f'<p class="b2a-cast-personality">{personality}</p>'
        f'<{d} class="b2a-cast-quote">'
        f'<{d} class="b2a-cast-quote-label">代表台词 1</{d}>'
        f'<{d} class="b2a-cast-quote-text">「{q1}」</{d}>'
        f'<{d} class="b2a-cast-quote-inst">语气：{q1i}</{d}>'
        f"</{d}>"
        f'<{d} class="b2a-cast-quote">'
        f'<{d} class="b2a-cast-quote-label">代表台词 2</{d}>'
        f'<{d} class="b2a-cast-quote-text">「{q2}」</{d}>'
        f'<{d} class="b2a-cast-quote-inst">语气：{q2i}</{d}>'
        f"</{d}>"
        f"</{d}>"
    )


def _auditions_session_key(card_key: str) -> str:
    return f"cast_auditions_{card_key}"


def _append_cast_audition(
    card_key: str,
    *,
    voice_id: str,
    voice_label: str,
    audio_bytes: bytes,
) -> None:
    """保留同一角色多个音色的试听，便于横向对比（同音色重复试听则覆盖）。"""
    key = _auditions_session_key(card_key)
    history: list[dict] = list(st.session_state.get(key) or [])
    history = [h for h in history if h.get("voice_id") != voice_id]
    history.append(
        {
            "voice_id": voice_id,
            "voice_label": voice_label,
            "audio_bytes": audio_bytes,
        }
    )
    st.session_state[key] = history


def _bound_cache_key(character_name: str) -> str:
    return f"cast_bound_voice::{character_name}"


def _render_cast_status_block(
    *,
    name: str,
    bound_voice: str,
    label_map: dict[str, str],
    voice_ids: list[str],
    card_key: str,
) -> None:
    """绑定状态：统一绿色条，常驻显示（不随试听或 rerun 消失）。"""
    bind_flash = st.session_state.pop(f"cast_bind_flash_{card_key}", None)
    cache = st.session_state.get(_bound_cache_key(name)) or {}

    effective_voice = (bound_voice or cache.get("voice_id") or "").strip()
    if not effective_voice and bind_flash:
        effective_voice = str(bind_flash.get("voice_id") or "").strip()

    if not effective_voice:
        return

    voice_label = html.escape(
        label_map.get(effective_voice, cache.get("voice_label") or effective_voice)
    )
    sync_lines = bind_flash.get("lines") if bind_flash else cache.get("lines")
    extras_n = int((bind_flash or {}).get("extras_characters_updated") or 0)
    sync_html = ""
    if bind_flash and bind_flash.get("is_stock_extra") and extras_n > 0:
        sync_html = (
            f'<span class="b2a-cast-sync-note">'
            f"已套用 {extras_n} 名龙套配角"
            f"</span>"
        )
    elif sync_lines is not None and int(sync_lines) > 0:
        sync_html = (
            f'<span class="b2a-cast-sync-note">'
            f"已同步 {int(sync_lines)} 条剧本行"
            f"</span>"
        )

    st.markdown(
        '<div class="b2a-cast-voice-bound">'
        f"<span>✓ 已绑定</span> <strong>{voice_label}</strong>"
        f"{sync_html}"
        "</div>",
        unsafe_allow_html=True,
    )

    if effective_voice not in voice_ids:
        st.warning(
            f"已绑定音色 `{effective_voice}` 不在当前官方清单中，请重新选择并绑定。"
        )


def _render_cast_audition_history(card_key: str) -> None:
    history: list[dict] = st.session_state.get(_auditions_session_key(card_key)) or []
    if not history:
        return
    st.caption("试听记录（可保留多个音色对比）")
    for entry in history:
        label = html.escape(str(entry.get("voice_label") or entry.get("voice_id") or ""))
        st.markdown(
            f'<p class="b2a-cast-audition-label">试听 · {label}</p>',
            unsafe_allow_html=True,
        )
        audio_bytes = entry.get("audio_bytes")
        if audio_bytes:
            st.audio(audio_bytes, format="audio/mp3")


def _render_cast_card(
    character: dict,
    *,
    voice_ids: list[str],
    voice_owners: dict[str, str],
    api_key: str,
    card_key: str,
) -> None:
    name = str(character.get("name") or "")
    quote_1 = str(character.get("quote_1") or "").strip()
    q1_inst = str(character.get("quote_1_instruction") or "").strip()
    bound_voice = str(character.get("voice_id") or "").strip()

    tone = _cast_tone_class(character)
    with st.container(border=True):
        st.markdown(
            f'<div class="b2a-cast-tone-marker {tone}"></div>',
            unsafe_allow_html=True,
        )
        st.markdown(_cast_card_html(character), unsafe_allow_html=True)

        if not voice_ids:
            st.error("内置音色库不可用。")
            return

        _, label_map = voice_select_options(
            st.session_state.system_voices,
            voice_owners=voice_owners,
            current_character=name,
        )

        if bound_voice:
            st.session_state[_bound_cache_key(name)] = {
                "voice_id": bound_voice,
                "voice_label": label_map.get(bound_voice, bound_voice),
                "lines": (st.session_state.get(_bound_cache_key(name)) or {}).get(
                    "lines"
                ),
            }

        _render_cast_status_block(
            name=name,
            bound_voice=bound_voice,
            label_map=label_map,
            voice_ids=voice_ids,
            card_key=card_key,
        )

        default_idx = (
            voice_ids.index(bound_voice)
            if bound_voice and bound_voice in voice_ids
            else 0
        )
        pick_col, btn_col = st.columns([2.2, 1])
        with pick_col:
            selected_voice = st.selectbox(
                "试镜音色",
                options=voice_ids,
                index=default_idx,
                format_func=lambda vid, m=label_map: m.get(vid, vid),
                key=f"cast_voice_{card_key}",
                label_visibility="collapsed",
            )
        with btn_col:
            preview_clicked = st.button(
                "🎵 试听人设初舞台",
                key=f"cast_preview_{card_key}",
                use_container_width=True,
            )

        if preview_clicked:
            if not quote_1:
                st.error("该角色尚无代表台词 1，无法试听。")
            else:
                with st.spinner("正在渲染试听音频…"):
                    try:
                        audio_bytes = synthesize_casting_preview(
                            api_key,
                            voice_id=selected_voice,
                            quote_text=quote_1,
                            emotion_instruction=q1_inst,
                        )
                        _append_cast_audition(
                            card_key,
                            voice_id=selected_voice,
                            voice_label=label_map.get(
                                selected_voice, selected_voice
                            ),
                            audio_bytes=audio_bytes,
                        )
                    except StepAudioError as exc:
                        st.error(str(exc))
                    except Exception as exc:
                        st.error(f"试听失败：{exc}")

        _render_cast_audition_history(card_key)

        if character.get("is_stock_extra"):
            st.caption(
                "绑定后按配角 **性别 + 年龄档** 自动套用至所有龙套（importance=extra），"
                "不覆盖主演与旁白。"
            )
        else:
            st.caption("绑定将使用上方下拉框当前选中的音色")
        if st.button(
            "🔗 绑定音色",
            key=f"cast_bind_{card_key}",
            type="primary",
            use_container_width=True,
        ):
            owner = voice_owners.get(selected_voice, "")
            if owner and owner != name:
                st.info(
                    f"提示：音色 `{selected_voice}` 已由 **{owner}** 占用；"
                    "系统不拦截，已按您的选择保存。"
                )
            with get_connection() as conn:
                stats = bind_character_voice(conn, name, selected_voice)
                conn.commit()
            voice_label = label_map.get(selected_voice, selected_voice)
            lines_updated = int(stats.get("script_lines_updated") or 0)
            extras_n = int(stats.get("extras_characters_updated") or 0)
            flash_lines = lines_updated
            if extras_n > 0:
                flash_lines = lines_updated if lines_updated else extras_n
            st.session_state[f"cast_bind_flash_{card_key}"] = {
                "voice_id": selected_voice,
                "voice_label": voice_label,
                "lines": flash_lines,
                "extras_characters_updated": extras_n,
                "is_stock_extra": bool(character.get("is_stock_extra")),
            }
            st.session_state[_bound_cache_key(name)] = {
                "voice_id": selected_voice,
                "voice_label": voice_label,
                "lines": lines_updated,
            }
            try:
                st.toast(
                    f"已绑定 {name} → {label_map.get(selected_voice, selected_voice)}",
                    icon="✅",
                )
            except Exception:
                pass
            st.rerun()


def _effective_bound_voice(character: dict) -> str:
    name = str(character.get("name") or "")
    vid = str(character.get("voice_id") or "").strip()
    if not vid:
        vid = str(
            (st.session_state.get(_bound_cache_key(name)) or {}).get("voice_id") or ""
        ).strip()
    return vid


def _voice_short_label(voice_id: str, voices: list[SystemVoice]) -> str:
    """折叠态小卡片：仅展示音色中文名，不含 voice_id 与占用提示。"""
    vid = (voice_id or "").strip()
    if not vid:
        return ""
    for v in voices:
        if v.voice_id == vid:
            return (v.display_name or vid).strip()
    return vid


def _line_count_label(character: dict) -> str:
    line_n = int(character.get("dialogue_lines") or 0)
    if character.get("is_narrator"):
        return f"旁白 {line_n} 行"
    if character.get("is_stock_extra"):
        return f"匹配配角 {line_n} 人"
    return f"对白 {line_n} 句"


def _render_compact_cast_chip(
    character: dict,
    *,
    voices: list[SystemVoice],
    is_stock: bool = False,
) -> str:
    name = html.escape(str(character.get("name") or ""))
    gender = html.escape(str(character.get("gender") or "—"))
    age = html.escape(str(character.get("age") or "—"))
    lines = html.escape(_line_count_label(character))
    voice_id = _effective_bound_voice(character)
    if voice_id:
        voice_text = html.escape(_voice_short_label(voice_id, voices))
        voice_cls = "chip-voice"
    else:
        voice_text = "未绑定"
        voice_cls = "chip-voice unbound"
    tone = _cast_tone_class(character, is_stock=is_stock)
    return (
        f'<div class="b2a-cast-compact-chip {tone}">'
        f'<div class="chip-name">{name}</div>'
        f'<div class="chip-meta">{gender} · {age} · {lines}</div>'
        f'<div class="{voice_cls}">音色：{voice_text}</div>'
        f"</div>"
    )


def _chip_sort_key(character: dict, *, is_stock: bool) -> tuple:
    tone_order = {
        "tone-narrator": 0,
        "tone-male-main": 1,
        "tone-female-main": 2,
        "tone-male-stock": 3,
        "tone-female-stock": 4,
    }
    tone = _cast_tone_class(character, is_stock=is_stock)
    return (
        tone_order.get(tone, 9),
        -int(character.get("dialogue_lines") or 0),
        str(character.get("name") or ""),
    )


def _render_compact_cast_overview(
    cast: list[dict],
    stock_cast: list[dict],
    *,
    voices: list[SystemVoice],
) -> None:
    """折叠态：平铺极简小卡片（按旁白→男主→女主→龙套排序）。"""
    entries: list[tuple[dict, bool]] = [
        (item, bool(item.get("is_stock_extra"))) for item in cast
    ]
    entries.extend((item, True) for item in stock_cast)
    entries.sort(key=lambda pair: _chip_sort_key(pair[0], is_stock=pair[1]))
    chips = [
        _render_compact_cast_chip(item, voices=voices, is_stock=is_stock)
        for item, is_stock in entries
    ]
    st.markdown(
        f'<div class="b2a-cast-compact-grid">{"".join(chips)}</div>',
        unsafe_allow_html=True,
    )
    st.caption("已折叠详细试镜控件；点击「展开试镜墙」可试听与重新绑定音色。")


def casting_binding_complete(conn) -> bool:
    """旁白 + 全部主演 + 六档龙套均已写入 voice_id。"""
    cast = fetch_main_cast_characters(conn)
    stock = fetch_stock_extra_characters(conn)
    if not cast or len(stock) < 1:
        return False
    for item in (*cast, *stock):
        if not str(item.get("voice_id") or "").strip():
            return False
    return True


def _count_bound_cast(cast: list[dict]) -> int:
    n = 0
    for c in cast:
        name = str(c.get("name") or "")
        vid = str(c.get("voice_id") or "").strip()
        if not vid:
            vid = str((st.session_state.get(_bound_cache_key(name)) or {}).get("voice_id") or "").strip()
        if vid:
            n += 1
    return n


def _persist_stock_bound_cache_to_db(
    conn,
    stock_cast: list[dict],
) -> None:
    """把 session 里已绑定的龙套音色写回数据库（避免仅 UI 缓存、标题不刷新）。"""
    for item in stock_cast:
        name = str(item.get("name") or "")
        if not name:
            continue
        cached = st.session_state.get(_bound_cache_key(name)) or {}
        vid = str(cached.get("voice_id") or "").strip()
        if not vid:
            continue
        if str(item.get("voice_id") or "").strip():
            continue
        conn.execute(
            "UPDATE characters SET voice_id = ? WHERE name = ?",
            (vid, name),
        )


def render_casting_room() -> None:
    """配音演员试镜大厅主面板。"""
    st.caption(
        f"**旁白**固定置顶；累计对白量 **Top {ROLLING_RANK_TOP_N}** 为主演试镜；"
        "**6 档龙套试镜** 覆盖其余配角。"
    )

    api_key = (st.session_state.get("step_api_key") or "").strip()
    if not api_key:
        st.warning("请先在侧边栏配置并验证 Step API Key（试听合成需要）。")

    ensure_bundled_voices_in_session()
    voices = st.session_state.get("system_voices") or []
    voice_err = st.session_state.get("system_voices_error") or ""
    if voice_err:
        st.error(voice_err)
    elif voices:
        st.caption(
            f"内置 StepAudio 2.5 官方音色 · **{len(voices)}** 个"
            "（[平台文档清单](https://platform.stepfun.com/docs/zh/guides/developer/tts)）"
        )

    with get_connection() as conn:
        stats = get_pipeline_stats(conn)
        cast = fetch_main_cast_characters(conn)
        stock_cast = fetch_stock_extra_characters(conn)
        _persist_stock_bound_cache_to_db(conn, stock_cast)
        stock_cast = fetch_stock_extra_characters(conn)
        voice_owners = list_character_voice_assignments(conn)
        binding_complete = casting_binding_complete(conn)
        conn.commit()

    if stats["script_lines"] == 0:
        st.info("剧本库尚无数据。请先完成上方「剧本智能拆解」或导入离线剧本 CSV 后再试镜。")
        return

    if not cast:
        st.warning(
            "当前没有标记为 **main** 的主演。"
            f"请先完成剧本拆解并等待 Top {ROLLING_RANK_TOP_N} 演员榜刷新，或检查 characters 表。"
        )
        return

    novel_name = (st.session_state.get("uploaded_novel_name") or "未命名").strip()
    with st.expander("💾 配音表离线备份", expanded=False):
        st.caption(
            "改一字重跑新书前，建议先导出；拆解完成后可从 JSON 一键恢复旁白 / 主演 / 龙套音色。"
        )
        bc1, bc2 = st.columns(2)
        with bc1:
            if st.button("导出配音备份到 backups/", key="casting_export_backup"):
                try:
                    jp, cp = export_casting_backup(novel_name)
                    st.success(f"已保存\n`{jp.name}`\n`{cp.name}`")
                except OSError as exc:
                    st.error(f"导出失败：{exc}")
        with bc2:
            backups = list_casting_backups(novel_name) or list_casting_backups()
            pick = st.selectbox(
                "从备份恢复",
                options=[""] + [p.name for p in backups],
                format_func=lambda x: x or "（选择 JSON）",
                key="casting_import_pick",
            )
            if st.button("导入选中备份", key="casting_import_backup", disabled=not pick):
                path = next((p for p in backups if p.name == pick), None)
                if path:
                    try:
                        stats = import_casting_backup(path)
                        st.success(
                            f"已写回 **{stats['characters_updated']}** 个角色 · "
                            f"**{stats['script_lines_updated']}** 行剧本 voice_id"
                        )
                        st.rerun()
                    except (OSError, ValueError) as exc:
                        st.error(f"导入失败：{exc}")

    bound_main = _count_bound_cast(cast)
    main_total = len(cast)
    bound_stock = _count_bound_cast(stock_cast)
    stock_total = len(stock_cast)

    if "casting_collapsed" not in st.session_state:
        # 未完成：首次进入默认展开；已全部绑定：下次打开默认折叠
        st.session_state.casting_collapsed = binding_complete

    summary_col, fold_col = st.columns([5, 1])
    with summary_col:
        st.markdown(
            f"主演 **{bound_main}/{main_total}** · "
            f"龙套试镜 **{bound_stock}/{stock_total}**"
        )
    with fold_col:
        fold_label = (
            "📂 展开试镜墙"
            if st.session_state.casting_collapsed
            else "📦 折叠试镜墙"
        )
        if st.button(fold_label, key="casting_collapse_btn", use_container_width=True):
            st.session_state.casting_collapsed = not st.session_state.casting_collapsed
            st.rerun()

    if bound_main == main_total and bound_stock == stock_total:
        st.success(
            "🎉 主演与龙套试镜音色均已配置；配角将按年龄/性别自动归入对应龙套档。"
        )
    else:
        pending: list[str] = []
        if bound_main < main_total:
            pending.append(f"主演 {main_total - bound_main} 名")
        if bound_stock < stock_total:
            pending.append(f"龙套试镜 {stock_total - bound_stock} 项")
        if pending:
            st.info(f"尚有 **{' · '.join(pending)}** 未绑定音色。")

    if not voices:
        return

    voice_ids, label_map = voice_select_options(
        voices,
        voice_owners=voice_owners,
        current_character="",
    )

    if st.session_state.casting_collapsed:
        _render_compact_cast_overview(cast, stock_cast, voices=voices)
        return

    narrators = [c for c in cast if c.get("is_narrator")]
    mains = [c for c in cast if not c.get("is_narrator")]
    top_name = mains[0].get("name", "—") if mains else "—"
    top_lines = int(mains[0].get("dialogue_lines") or 0) if mains else 0
    cast_summary = (
        f"旁白 + **{len(mains)}** 位主演"
        if narrators
        else f"**{len(mains)}** 位主演"
    )
    st.caption(
        f"已加载 {cast_summary}（主演最高 **{top_name}** · {top_lines} 句对白）· "
        f"剧本共 **{stats['script_lines']:,}** 行"
    )

    if not api_key:
        return

    cards_per_row = 3

    st.markdown("#### 🎭 主演试镜")
    for row_start in range(0, len(cast), cards_per_row):
        cols = st.columns(cards_per_row)
        for col_idx, col in enumerate(cols):
            idx = row_start + col_idx
            if idx >= len(cast):
                break
            with col:
                _render_cast_card(
                    cast[idx],
                    voice_ids=voice_ids,
                    voice_owners=voice_owners,
                    api_key=api_key,
                    card_key=f"{cast[idx].get('name', idx)}_{idx}",
                )

    st.divider()
    st.markdown("#### 🎙️ 龙套试镜")
    st.caption(
        "为非主演配角按 **性别 + 年龄（少年 / 青年 / 中老年）** 自动分档；"
        "绑定某一档后，会写入所有匹配的 **extra** 配角及其剧本对白行，**不覆盖** 主演与旁白。"
    )
    for row_start in range(0, len(stock_cast), cards_per_row):
        cols = st.columns(cards_per_row)
        for col_idx, col in enumerate(cols):
            idx = row_start + col_idx
            if idx >= len(stock_cast):
                break
            with col:
                item = stock_cast[idx]
                _render_cast_card(
                    item,
                    voice_ids=voice_ids,
                    voice_owners=voice_owners,
                    api_key=api_key,
                    card_key=f"stock_{item.get('name', idx)}",
                )
