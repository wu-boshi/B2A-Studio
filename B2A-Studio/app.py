"""
B2A-Studio (Book-to-Audio Studio) — Phase 1 entry point.

Local Streamlit UI: API credential management, legal disclaimer gate,
and .txt novel import with basic validation.
"""

from __future__ import annotations

import re
import os
from pathlib import Path
from typing import Literal

import threading
import time
from queue import Empty, Queue

import requests
import streamlit as st
from dotenv import load_dotenv, set_key

from db import (
    ROLLING_RANK_TOP_N,
    ensure_database,
    count_pending_blocked_segments,
    delete_script_line_by_chapter_line_idx,
    delete_script_line_by_id,
    fetch_characters_preview,
    fetch_script_lines_for_edit,
    fetch_script_lines_preview,
    database_has_other_pipeline_progress,
    insert_blank_script_line_at,
    insert_script_line_manual,
    list_pending_blocked_segments,
    PERSONALITY_MAX_CHARS,
    get_connection,
    get_pipeline_stats,
    list_script_chapters,
    novel_has_pipeline_progress,
    resolve_blocked_segment,
    reset_database,
    renumber_chapter_line_idx,
    update_character_by_name,
    update_script_line_by_id,
)
import pandas as pd

from pipeline import (
    CHAPTER_COVERAGE_MIN,
    CHAPTER_COVERAGE_MAX,
    LLM_MAX_TOKENS,
    LLM_MODEL,
    ROUTER_MAX_TOKENS,
    CHAPTER_READ_TIMEOUT_SEC,
    CHAPTER_SINGLE_SHOT_MAX,
    FALLBACK_CHAPTER_OVERLAP,
    FALLBACK_CHAPTER_SIZE,
    compact_fragmented_script_lines,
    condense_overlong_personalities,
    enrich_incomplete_characters,
    list_incomplete_character_names,
    prune_spurious_characters,
    sync_speaking_roles_to_cast,
    ROUTER_MAX_TOKENS_CAP,
    audit_chapter_script_duplicates,
    compare_text_coverage,
    compute_novel_chapter_coverage_report,
    compute_novel_fingerprint,
    compute_novel_fingerprint_legacy,
    normalize_content_fingerprint,
    audit_novel_chapter_integrity,
    estimate_chunk_count,
    get_local_book_progress,
    process_novel_pipeline,
    step_plan_pipeline_endpoint,
    PIPELINE_FAILURE_RETRY_WAIT_SEC,
)
from utils.pipeline_log import LOG_FILE as PIPELINE_LOG_FILE
from utils.audiobook_ffmpeg import FFmpegNotAvailable, ensure_ffmpeg_configured

try:
    ensure_ffmpeg_configured()
except FFmpegNotAvailable:
    pass

from utils.casting_ui import (
    casting_binding_complete,
    ensure_bundled_voices_in_session,
    render_casting_room,
)
from utils.pronunciation_ui import (
    render_pronunciation_panel,
    render_pronunciation_recording_hint,
)
from utils.recording_ui import render_audiobook_recording_studio
from utils.script_csv_io import (
    ScriptCsvImportError,
    export_offline_csv_bytes,
    import_script_csv_bytes,
)
from utils.ui_scroll import (
    ANCHOR_CASTING,
    ANCHOR_PRONUNCIATION,
    ANCHOR_RECORDING,
    apply_pending_scroll,
    render_scroll_anchor,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
from utils.b2a_paths import APP_DIR, B2A_ROOT
ENV_FILE = APP_DIR / ".env"
ENV_KEY_NAME = "STEP_API_KEY"

# StepPlan 订阅制 API（与 OpenAI SDK 的 base_url 一致，勿使用 /v1/ 按 Token 计费端点）
STEP_API_BASE = "https://api.stepfun.com/step_plan/v1"
STEP_CHAT_URL = f"{STEP_API_BASE}/chat/completions"
STEP_SPEECH_URL = f"{STEP_API_BASE}/audio/speech"  # Phase 4 TTS
STEP_VERIFY_MODEL = "step-router-v1"

STEP_PLAN_URL = "https://platform.stepfun.com/step-plan"
STEP_INTERFACE_KEY_URL = "https://platform.stepfun.com/interface-key"

PREVIEW_CHAR_LIMIT = 1000
REQUEST_TIMEOUT_SEC = 60
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB

VerifyStatus = Literal["ok", "quota", "invalid", "error"]

LEGAL_DISCLAIMER_TEXT = (
    "【版权与中立性声明】\n"
    "1. 本软件属于中立的本地技术辅助工具，本身不存储、不传播、亦不提供任何受著作权法保护的文本或音频内容。\n"
    "2. 用户上传、导入及处理的所有文本素材，须为用户依法享有著作权、或已取得著作权人合法授权（包括但不限于复制权、改编权、翻译权及汇编权等许可）的内容，或属于已进入公有领域（Public Domain）的公版作品。因用户处理未经授权的侵权作品而导致的一切法律纠纷、侵权责任及损失，均由使用者本人承担全部法律责任，本软件及开发者不承担任何连带或侵权责任。\n\n"
    "【第三方独立性声明】\n"
    "3. 本软件为独立开源项目，与上海阶跃星辰智能科技有限公司（包含其关联方，以下统称“阶跃星辰/StepFun”）不存在任何关联关系或商业合作关系。\n"
    "4. 本软件仅作为技术接口中转工具，鼓励并引导用户出于个人合规研究或学习目的，自行前往阶跃星辰开放平台（https://platform.stepfun.com/step-plan）自由决定是否订阅 StepPlan 套餐。有声书制作功能完全基于用户在本地输入的个人 API Key，调用由 StepPlan 原生支持的标准化接口实现。"
)
LEGAL_AGREE_CHECKBOX_LABEL = "我已阅读并同意以上全部声明条款"


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def load_api_key_from_env() -> str:
    """Load Step API Key from local .env file (empty string if missing)."""
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE, override=True)
    return os.getenv(ENV_KEY_NAME, "").strip()


def save_api_key_to_env(api_key: str) -> None:
    """Persist Step API Key to local .env (creates file if needed)."""
    ENV_FILE.touch(exist_ok=True)
    set_key(str(ENV_FILE), ENV_KEY_NAME, api_key)


def init_session_state() -> None:
    """Initialize Streamlit session_state keys used across reruns."""
    if "step_api_key" not in st.session_state:
        st.session_state.step_api_key = load_api_key_from_env()
    if "legal_agreed" not in st.session_state:
        st.session_state.legal_agreed = False
    if "api_verified" not in st.session_state:
        st.session_state.api_verified = False
    if "api_verify_status" not in st.session_state:
        st.session_state.api_verify_status = ""
    if "novel_fingerprint" not in st.session_state:
        st.session_state.novel_fingerprint = ""
    if "novel_content_fingerprint" not in st.session_state:
        st.session_state.novel_content_fingerprint = ""
    if "pipeline_result" not in st.session_state:
        st.session_state.pipeline_result = None
    if "pipeline_log_lines" not in st.session_state:
        st.session_state.pipeline_log_lines = []
    if "pipeline_log_text" not in st.session_state:
        st.session_state.pipeline_log_text = ""
    if "chapter_integrity_ack" not in st.session_state:
        st.session_state.chapter_integrity_ack = False
    if "pipeline_running" not in st.session_state:
        st.session_state.pipeline_running = False
    if "system_voices" not in st.session_state:
        st.session_state.system_voices = []
    if "system_voices_error" not in st.session_state:
        st.session_state.system_voices_error = ""


# ---------------------------------------------------------------------------
# API validation (StepPlan chat/completions — same as OpenAI client base_url)
# ---------------------------------------------------------------------------
def _parse_error_type(response: requests.Response) -> str:
    """Extract Stepfun error.type from JSON body when present."""
    try:
        body = response.json()
        return str(body.get("error", {}).get("type", ""))
    except (ValueError, AttributeError):
        return ""


def verify_step_api_key(api_key: str) -> tuple[VerifyStatus, str]:
    """
    Validate StepPlan API Key via POST {STEP_API_BASE}/chat/completions.

    Equivalent to:
        OpenAI(api_key=..., base_url="https://api.stepfun.com/step_plan/v1")
        .chat.completions.create(model="step-router-v1", messages=[...])

    HTTP 200  → key valid, StepPlan 可用
    HTTP 401  → invalid / revoked key
    HTTP 402  → key valid, StepPlan quota exhausted
    """
    if not api_key.strip():
        return "error", "请先输入 Stepfun API Key。"

    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": STEP_VERIFY_MODEL,
        "messages": [{"role": "user", "content": "你好，请用一句话确认连接正常。"}],
        "max_tokens": 32,
    }

    try:
        response = requests.post(
            STEP_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT_SEC,
        )
    except requests.exceptions.Timeout:
        return "error", "请求超时，请检查网络连接后重试。"
    except requests.exceptions.ConnectionError:
        return "error", "无法连接到 StepPlan API 服务器，请检查网络。"
    except requests.exceptions.RequestException as exc:
        return "error", f"网络请求异常：{exc}"

    if response.status_code == 200:
        try:
            body = response.json()
            if body.get("choices"):
                return "ok", "API 凭证验证成功，StepPlan 连接正常！"
        except ValueError:
            pass
        return "ok", "API 凭证验证成功，StepPlan 连接正常！"

    if response.status_code == 401:
        return "invalid", "凭证失效，请检查您的 Stepfun API Key（需为 StepPlan 接口密钥）"

    if response.status_code == 402 or _parse_error_type(response) == "quota_exceeded":
        return (
            "quota",
            "API 密钥有效，但当前 StepPlan 额度已用尽。"
            "请前往开放平台查看用量；额度恢复后即可正常使用。",
        )

    if response.status_code == 429:
        return (
            "quota",
            "API 密钥有效，但当前请求频率超限（HTTP 429）。请稍后重试。",
        )

    try:
        detail = response.json()
    except ValueError:
        detail = response.text[:200] if response.text else ""

    return (
        "error",
        f"连接异常（HTTP {response.status_code}）。"
        f"{' ' + str(detail) if detail else ''}",
    )


# ---------------------------------------------------------------------------
# Text file handling
# ---------------------------------------------------------------------------
def decode_uploaded_text(raw_bytes: bytes) -> tuple[str, str]:
    """Decode uploaded .txt bytes; try UTF-8 then common Chinese encodings."""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return raw_bytes.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError(
        "unknown",
        raw_bytes[:32],
        0,
        1,
        "无法识别文件编码，请另存为 UTF-8 后重试。",
    )


def on_novel_loaded(name: str, text: str) -> None:
    """Persist novel in session; reset SQLite only when正文指纹真正变化。"""
    fp = compute_novel_fingerprint(text, name)
    legacy_fp = compute_novel_fingerprint_legacy(text, name)
    fp_norm = normalize_content_fingerprint(fp)
    prev_norm = normalize_content_fingerprint(
        st.session_state.get("novel_content_fingerprint")
        or st.session_state.get("novel_fingerprint", "")
    )

    if prev_norm == fp_norm:
        st.session_state.novel_fingerprint = fp
        st.session_state.novel_content_fingerprint = fp_norm
        st.session_state.uploaded_novel_text = text
        st.session_state.uploaded_novel_name = name
        return

    ensure_database()
    with get_connection() as conn:
        has_this_book = novel_has_pipeline_progress(conn, fp, legacy_fp)
        has_other_book = database_has_other_pipeline_progress(conn, fp, legacy_fp)

    if not prev_norm and has_this_book:
        # 刷新页面后重传同一本书：保留本地剧本，勿因 session 为空而清库
        st.session_state.novel_fingerprint = fp
        st.session_state.novel_content_fingerprint = fp_norm
        st.session_state.uploaded_novel_text = text
        st.session_state.uploaded_novel_name = name
        st.session_state.chapter_integrity_ack = False
        return

    if prev_norm and prev_norm != fp_norm:
        reset_database()
        st.session_state.pipeline_result = None
    elif not prev_norm and has_other_book:
        reset_database()
        st.session_state.pipeline_result = None

    st.session_state.novel_fingerprint = fp
    st.session_state.novel_content_fingerprint = fp_norm
    st.session_state.uploaded_novel_text = text
    st.session_state.uploaded_novel_name = name
    st.session_state.chapter_integrity_ack = False


def read_uploaded_novel(uploaded_file) -> tuple[str, str]:
    """Stream-read uploaded file and return decoded text plus encoding label."""
    raw = uploaded_file.getvalue()
    return decode_uploaded_text(raw)


def format_disclaimer_html(text: str) -> str:
    """Turn plain-text disclaimer into styled HTML paragraphs."""
    blocks: list[str] = []
    for block in text.strip().split("\n\n"):
        lines = block.strip().split("\n")
        if not lines:
            continue
        first = lines[0]
        if first.startswith("【") and first.endswith("】"):
            blocks.append(f"<p><strong>{first}</strong></p>")
            body_lines = lines[1:]
        else:
            body_lines = lines
        if body_lines:
            body = "<br>".join(body_lines)
            blocks.append(f"<p>{body}</p>")
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# UI styling (Phase 1 original — clean light layout)
# ---------------------------------------------------------------------------
def inject_custom_css() -> None:
    """Lightweight custom styles for a cleaner, modern layout."""
    st.markdown(
        """
        <style>
        .b2a-main-title {
            font-size: 2rem;
            font-weight: 700;
            background: linear-gradient(90deg, #1e3a5f 0%, #2563eb 55%, #7c3aed 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.25rem;
        }
        .b2a-subtitle {
            color: #64748b;
            font-size: 0.95rem;
            margin-bottom: 1.5rem;
        }
        .b2a-legal-box {
            border: 1px solid #f59e0b;
            background: linear-gradient(135deg, #fffbeb 0%, #fef3c7 100%);
            border-radius: 12px;
            padding: 1rem 1.25rem;
            margin-bottom: 1.25rem;
        }
        .b2a-legal-box h4 {
            color: #b45309;
            margin: 0 0 0.5rem 0;
            font-size: 1rem;
        }
        .b2a-legal-box p {
            color: #78350f;
            font-size: 0.875rem;
            line-height: 1.6;
            margin: 0.35rem 0;
        }
        div[data-testid="stMetricValue"] {
            font-size: 1.35rem;
        }
        /* 上传框：隐藏英文默认文案，改中文且副提示不换行 */
        [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzoneInstructions"] {
            font-size: 0 !important;
            line-height: 1.4;
            white-space: nowrap;
        }
        [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzoneInstructions"]::before {
            content: "将文件拖拽到此处，或点击选择";
            display: block;
            font-size: 0.95rem;
            color: #31333f;
            white-space: nowrap;
        }
        [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzoneInstructions"] small {
            visibility: hidden;
            position: relative;
            display: block;
            height: 1.15em;
            margin-top: 0.1rem;
            overflow: visible;
        }
        [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzoneInstructions"] small::after {
            visibility: visible;
            position: absolute;
            left: 0;
            top: 0;
            content: "单文件 · 最大 5 MB · 仅支持 TXT";
            color: #6b7280;
            font-size: 0.8rem;
            white-space: nowrap;
        }
        [data-testid="stFileUploader"] button p {
            font-size: 0 !important;
            line-height: 1.3;
        }
        [data-testid="stFileUploader"] button p::after {
            content: "浏览本地文件并上传";
            font-size: 0.8125rem;
            color: rgb(49, 51, 63);
            white-space: nowrap;
        }
        /* 运行日志：expander 内 st.code，约 20 行高、纵向滚动、自动换行 */
        [data-testid="stVerticalBlock"]:has(.b2a-pipeline-log-anchor)
            div[data-testid="stCodeBlock"] {
            max-height: 20em;
            overflow: hidden;
            border: 1px solid rgba(49, 51, 63, 0.2);
            border-radius: 0.5rem;
        }
        [data-testid="stVerticalBlock"]:has(.b2a-pipeline-log-anchor)
            div[data-testid="stCodeBlock"] pre {
            max-height: 20em;
            overflow: auto !important;
            margin: 0;
            padding: 0.75rem 1rem;
            font-size: 0.8125rem;
            line-height: 1.45;
            white-space: pre-wrap !important;
            word-break: break-word;
            overflow-wrap: anywhere;
        }
        /* 铸模导演墙人物卡片 */
        .b2a-cast-card {
            border-radius: 12px;
            padding: 0.15rem 0 0.5rem 0;
        }
        .b2a-cast-card-head {
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            gap: 0.5rem;
            margin-bottom: 0.5rem;
        }
        .b2a-cast-name {
            font-size: 1.15rem;
            font-weight: 700;
            color: #1e3a5f;
        }
        .b2a-cast-meta {
            font-size: 0.8rem;
            color: #64748b;
            white-space: nowrap;
        }
        .b2a-cast-personality {
            font-size: 0.85rem;
            line-height: 1.55;
            color: #334155;
            background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
            border-left: 3px solid #6366f1;
            padding: 0.55rem 0.75rem;
            border-radius: 0 8px 8px 0;
            margin: 0 0 0.65rem 0;
        }
        .b2a-cast-quote {
            margin: 0.45rem 0;
            padding: 0.5rem 0.65rem;
            background: #fffbeb;
            border: 1px solid #fde68a;
            border-radius: 8px;
        }
        .b2a-cast-quote-label {
            font-size: 0.72rem;
            font-weight: 600;
            color: #b45309;
            margin-bottom: 0.2rem;
        }
        .b2a-cast-quote-text {
            font-size: 0.9rem;
            color: #78350f;
            font-weight: 500;
        }
        .b2a-cast-quote-inst {
            font-size: 0.78rem;
            color: #a16207;
            margin-top: 0.25rem;
        }
        .b2a-cast-voice-bound {
            display: flex;
            align-items: center;
            gap: 0.35rem;
            margin: 0.35rem 0 0.65rem 0;
            padding: 0.45rem 0.65rem;
            font-size: 0.86rem;
            color: #166534;
            background: #ecfdf5;
            border: 1px solid #86efac;
            border-radius: 8px;
        }
        .b2a-cast-voice-bound strong {
            font-weight: 600;
            color: #14532d;
        }
        .b2a-cast-sync-note {
            margin-left: 0.35rem;
            font-size: 0.78rem;
            color: #15803d;
            font-weight: 500;
        }
        .b2a-cast-audition-label {
            font-size: 0.78rem;
            color: #64748b;
            margin: 0.5rem 0 0.15rem 0;
        }
        .b2a-scroll-anchor {
            scroll-margin-top: 5.5rem;
        }
        .b2a-section-recording {
            scroll-margin-top: 5.5rem;
        }
        .b2a-recording-panels-marker {
            display: none;
        }
        [data-testid="stVerticalBlockBorderWrapper"]:has(#b2a-recording-studio) hr {
            display: none !important;
            margin: 0 !important;
        }
        [data-testid="stVerticalBlockBorderWrapper"]:has(#b2a-recording-studio)
            [data-testid="stExpander"] {
            margin-top: 0 !important;
            margin-bottom: 0.75rem !important;
        }
        [data-testid="stVerticalBlockBorderWrapper"]:has(#b2a-recording-studio)
            [data-testid="stExpander"]:last-of-type {
            margin-bottom: 0 !important;
        }
        .b2a-cast-compact-grid {
            display: flex;
            flex-wrap: wrap;
            gap: 0.55rem;
            margin: 0.5rem 0 0.25rem 0;
        }
        .b2a-cast-compact-chip {
            flex: 1 1 11.5rem;
            max-width: 100%;
            border: 1px solid #e2e8f0;
            border-radius: 10px;
            padding: 0.55rem 0.7rem;
            background: linear-gradient(135deg, #fafbfc 0%, #f8fafc 100%);
            font-size: 0.8rem;
            line-height: 1.45;
        }
        .b2a-cast-compact-chip.tone-narrator,
        .b2a-cast-card.tone-narrator {
            border-color: #d4d4d8;
            background: linear-gradient(135deg, #f4f4f5 0%, #e4e4e7 100%);
        }
        .b2a-cast-compact-chip.tone-male-main,
        .b2a-cast-card.tone-male-main {
            border-color: #c7d2fe;
            background: linear-gradient(135deg, #f5f7ff 0%, #eef2ff 100%);
        }
        .b2a-cast-compact-chip.tone-female-main {
            border-color: #fda4af;
            background: linear-gradient(135deg, #fff5f7 0%, #ffe4e8 100%);
        }
        .b2a-cast-card.tone-female-main {
            border-color: #c7d2fe;
            background: linear-gradient(135deg, #f5f7ff 0%, #eef2ff 100%);
        }
        .b2a-cast-compact-chip.tone-male-stock,
        .b2a-cast-card.tone-male-stock {
            border-color: #e0e7ff;
            background: linear-gradient(135deg, #f8faff 0%, #f1f5ff 100%);
        }
        .b2a-cast-compact-chip.tone-female-stock {
            border-color: #fecdd3;
            background: linear-gradient(135deg, #fffbfb 0%, #fff0f2 100%);
        }
        .b2a-cast-card.tone-female-stock {
            border-color: #e0e7ff;
            background: linear-gradient(135deg, #f8faff 0%, #f1f5ff 100%);
        }
        [data-testid="stVerticalBlockBorderWrapper"]:has(.b2a-cast-tone-marker.tone-narrator) {
            background: linear-gradient(135deg, #f4f4f5 0%, #e4e4e7 100%) !important;
            border-color: #d4d4d8 !important;
        }
        [data-testid="stVerticalBlockBorderWrapper"]:has(.b2a-cast-tone-marker.tone-male-main) {
            background: linear-gradient(135deg, #f5f7ff 0%, #eef2ff 100%) !important;
            border-color: #c7d2fe !important;
        }
        [data-testid="stVerticalBlockBorderWrapper"]:has(.b2a-cast-tone-marker.tone-female-main) {
            background: linear-gradient(135deg, #f5f7ff 0%, #eef2ff 100%) !important;
            border-color: #c7d2fe !important;
        }
        [data-testid="stVerticalBlockBorderWrapper"]:has(.b2a-cast-tone-marker.tone-male-stock) {
            background: linear-gradient(135deg, #f8faff 0%, #f1f5ff 100%) !important;
            border-color: #e0e7ff !important;
        }
        [data-testid="stVerticalBlockBorderWrapper"]:has(.b2a-cast-tone-marker.tone-female-stock) {
            background: linear-gradient(135deg, #f8faff 0%, #f1f5ff 100%) !important;
            border-color: #e0e7ff !important;
        }
        .b2a-cast-tone-marker {
            display: none;
        }
        .b2a-cast-compact-chip .chip-name {
            font-weight: 700;
            color: #1e3a5f;
            font-size: 0.88rem;
            margin-bottom: 0.15rem;
        }
        .b2a-cast-compact-chip .chip-meta {
            color: #64748b;
            font-size: 0.76rem;
        }
        .b2a-cast-compact-chip .chip-voice {
            margin-top: 0.25rem;
            font-size: 0.76rem;
            color: #1e293b;
            font-weight: 500;
        }
        .b2a-cast-compact-chip .chip-voice.unbound {
            color: #b45309;
            font-weight: 500;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Sidebar: credential center
# ---------------------------------------------------------------------------
def render_sidebar() -> None:
    """Render API key input, persistence, and connection verification."""
    with st.sidebar:
        st.header("🔐 凭证配置中心")
        st.caption("Stepfun 统一 API Key（文字 + 音频共用）")
        st.markdown(
            f"可在订阅 [StepPlan]({STEP_PLAN_URL}) 之后，"
            f"创建对应的 [接口密钥]({STEP_INTERFACE_KEY_URL})。",
        )

        prev_key = st.session_state.step_api_key
        st.text_input(
            "Step API Key",
            type="password",
            placeholder="xxxxxxxxxxxxxxxx",
            help="密钥仅保存在本机 .env 文件中，不会上传至第三方服务器。",
            key="step_api_key",
        )

        if st.session_state.step_api_key != prev_key:
            st.session_state.api_verified = False
            st.session_state.api_verify_status = ""
            st.session_state.system_voices = []
            st.session_state.system_voices_error = ""
            try:
                save_api_key_to_env(st.session_state.step_api_key)
            except OSError as exc:
                st.warning(f"无法写入本地 .env：{exc}")

        st.divider()

        if st.button("验证连接", type="primary", use_container_width=True):
            with st.spinner("正在通过 StepPlan 验证 API Key…"):
                status, message = verify_step_api_key(st.session_state.step_api_key)
            st.session_state.api_verify_status = status
            st.session_state.api_verified = status in ("ok", "quota")

            if status == "ok":
                st.success(message)
            elif status == "quota":
                st.warning(message)
                st.markdown(
                    f"[打开 StepPlan 控制台]({STEP_PLAN_URL}) · "
                    f"[管理接口密钥]({STEP_INTERFACE_KEY_URL})"
                )
            elif status == "invalid":
                st.error(message)
            else:
                st.error(message)

        if st.session_state.api_verified:
            if st.session_state.api_verify_status == "quota":
                st.caption("⚠️ 密钥有效，等待额度恢复")
            else:
                st.caption("✅ 最近一次验证已通过")

        st.divider()
        st.caption("本程序在您电脑上运行。")
        st.caption("仅关闭浏览器标签不会停止服务；请点击下方按钮退出。")
        if st.button("完全退出", use_container_width=True, help="结束本机 B2A-Studio 服务（等同于关闭启动终端）"):
            import os
            import signal

            st.warning("正在关闭本机服务…")
            os.kill(os.getpid(), signal.SIGTERM)


# ---------------------------------------------------------------------------
# Phase 2: pipeline controls
# ---------------------------------------------------------------------------
PIPELINE_UI_INACTIVITY_SEC = CHAPTER_READ_TIMEOUT_SEC
_pipeline_worker_thread: threading.Thread | None = None


def _pipeline_worker_alive() -> bool:
    global _pipeline_worker_thread
    return (
        _pipeline_worker_thread is not None
        and _pipeline_worker_thread.is_alive()
    )


def _sync_pipeline_busy_state() -> bool:
    """若 session 标记在跑但线程已结束，自动清掉 stale 状态。"""
    if st.session_state.get("pipeline_running") and not _pipeline_worker_alive():
        st.session_state.pipeline_running = False
    return bool(st.session_state.get("pipeline_running")) and _pipeline_worker_alive()

CHARACTER_TABLE_HEADERS = {
    "name": "角色",
    "dialogue_lines": "对白行数",
    "gender": "性别",
    "age": "年龄",
    "personality": "人设",
    "quote_1": "代表台词1",
    "quote_2": "代表台词2",
    "quote_1_instruction": "台词1情绪",
    "quote_2_instruction": "台词2情绪",
    "voice_id": "音色ID",
}

SCRIPT_TABLE_HEADERS = {
    "chapter_num": "章",
    "line_idx": "行号",
    "role": "角色",
    "emotion_instruction": "语气指令",
    "content": "正文",
    "is_dialogue": "是否对白",
}

def _dataframe_chinese_columns(
    rows: list[dict],
    header_map: dict[str, str],
) -> pd.DataFrame:
    """按 header_map 顺序选取列并统一改为中文表头。"""
    if not rows:
        return pd.DataFrame(columns=[header_map[k] for k in header_map])
    df = pd.DataFrame(rows)
    ordered = [k for k in header_map if k in df.columns]
    df = df[ordered].rename(columns=header_map)
    if "是否对白" in df.columns:
        df["是否对白"] = df["是否对白"].map(
            lambda x: "是"
            if x in (1, True, "1", "true", "True")
            else ("否" if x in (0, False, "0", "false", "False") else x)
        )
    return df


def _script_export_filename(chapter_num: int | None) -> str:
    if chapter_num is not None:
        return f"剧本_第{chapter_num}章.csv"
    return "剧本_全书.csv"


def _rows_to_csv_bytes(rows: list[dict], header_map: dict[str, str]) -> bytes:
    return _dataframe_chinese_columns(rows, header_map).to_csv(index=False).encode(
        "utf-8-sig"
    )


_LOG_LINE_SPLIT_RE = re.compile(r"(?=\d{2}:\d{2}:\d{2} \[)")
_FILE_LOG_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}),\d+ (\[(?:INFO|WARN|ERROR)\] .+)$"
)


def _normalize_pipeline_log_lines(lines: list[str]) -> list[str]:
    """拆成逐行条目（修复偶发多条日志粘在同一字符串里）。"""
    out: list[str] = []
    for item in lines:
        text = (item or "").strip()
        if not text:
            continue
        parts = _LOG_LINE_SPLIT_RE.split(text)
        if len(parts) <= 1:
            out.append(text)
            continue
        for part in parts:
            part = part.strip()
            if part:
                out.append(part)
    return out


def _file_log_line_to_ui(line: str) -> str:
    """将 logs/pipeline.log 行转为页面日志格式。"""
    match = _FILE_LOG_LINE_RE.match(line.strip())
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return line.strip()


def _sync_pipeline_log_from_file(
    offset: int,
    seen: set[str],
) -> tuple[int, list[str]]:
    """读取 pipeline.log 新增内容（断网期间后台仍写文件时，前台可追显示）。"""
    if not PIPELINE_LOG_FILE.exists():
        return offset, []
    try:
        with open(PIPELINE_LOG_FILE, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
            new_offset = fh.tell()
    except OSError:
        return offset, []
    fresh: list[str] = []
    for raw in data.decode("utf-8", errors="replace").splitlines():
        ui_line = _file_log_line_to_ui(raw)
        if not ui_line or ui_line in seen:
            continue
        seen.add(ui_line)
        fresh.append(ui_line)
    return new_offset, fresh


def _ensure_multiline_log_body(body: str) -> str:
    """保证每条日志独占一行（修复 session 中单行粘连）。"""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    if "\n" in text:
        return text
    return "\n".join(_normalize_pipeline_log_lines([text]))


def _render_scrollable_log_body(body: str) -> None:
    """约 20 行高的可滚动日志正文（配合外层 st.expander 折叠）。"""
    display = _ensure_multiline_log_body(body) or "（等待日志…）"
    st.markdown(
        '<span class="b2a-pipeline-log-anchor" aria-hidden="true"></span>',
        unsafe_allow_html=True,
    )
    st.code(display, language=None)


def _render_pipeline_log_box(container, lines: list[str]) -> None:
    """展示全部运行日志；可视区域约 20 行高，框内可上下滚动。"""
    lines = _normalize_pipeline_log_lines(lines)
    body = _ensure_multiline_log_body("\n".join(lines))
    st.session_state.pipeline_log_text = body
    with container.container():
        st.caption(f"共 {len(lines)} 条 · 框内可上下滚动查看全部")
        _render_scrollable_log_body(body)


def _wrap_columns_for_headers(
    english_keys: frozenset[str],
    header_map: dict[str, str],
) -> frozenset[str]:
    return frozenset(header_map.get(k, k) for k in english_keys)


def _render_scrollable_html_table(
    df: pd.DataFrame,
    wrap_columns: frozenset[str],
) -> None:
    """HTML 表格：长列自动换行，容器可纵横滚动。"""
    import html as html_module

    if df.empty:
        st.caption("（无数据）")
        return

    cols = list(df.columns)
    parts = [
        '<div style="max-height:min(75vh,960px);overflow:auto;'
        'border:1px solid rgba(49,51,63,0.2);border-radius:0.5rem">',
        '<table style="width:max-content;min-width:100%;border-collapse:collapse;'
        'font-size:0.875rem;line-height:1.5">',
        "<thead><tr>",
    ]
    for col in cols:
        parts.append(
            '<th style="position:sticky;top:0;z-index:1;background:#f8f9fb;'
            "padding:0.5rem 0.75rem;border-bottom:1px solid #ddd;"
            'text-align:left;white-space:nowrap">'
            f"{html_module.escape(str(col))}</th>"
        )
    parts.append("</tr></thead><tbody>")

    for _, row in df.iterrows():
        parts.append("<tr>")
        for col in cols:
            raw = row[col]
            cell = "" if pd.isna(raw) else str(raw)
            esc = html_module.escape(cell)
            style = (
                "padding:0.5rem 0.75rem;border-bottom:1px solid #eee;"
                "vertical-align:top"
            )
            if col in wrap_columns:
                style += (
                    ";white-space:pre-wrap;word-break:break-word;"
                    "min-width:10rem;max-width:36rem"
                )
            else:
                style += ";white-space:nowrap"
            parts.append(f'<td style="{style}">{esc}</td>')
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def _coverage_status_color(ratio: float) -> str:
    if ratio >= 0.92:
        return "#16a34a"
    if ratio >= 0.75:
        return "#ca8a04"
    return "#dc2626"


def render_chapter_coverage_panel(novel_text: str, *, compact: bool = False) -> None:
    """按章展示剧本 content（去标点）相对原文的覆盖率。"""
    if not (novel_text or "").strip():
        st.caption("上传当前小说 .txt 后，可在此查看各章覆盖率（去标点字数对比）。")
        return

    with get_connection() as conn:
        report = compute_novel_chapter_coverage_report(novel_text, conn)
    if not report:
        return

    line_counts: dict[int, int] = {}
    with get_connection() as conn:
        for row in report:
            ch = int(row["chapter_num"])
            n_lines = conn.execute(
                "SELECT COUNT(*) FROM script_lines WHERE chapter_num = ?",
                (ch,),
            ).fetchone()[0]
            line_counts[ch] = int(n_lines)

    rows_for_df = []
    for row in report:
        ch = int(row["chapter_num"])
        n_lines = line_counts.get(ch, 0)
        if compact and n_lines == 0:
            continue
        ratio = float(row["ratio"])
        rows_for_df.append(
            {
                "章": ch,
                "剧本行数": n_lines,
                "原文(去标点)": int(row["source_no_punct"]),
                "剧本(去标点)": int(row["script_no_punct"]),
                "覆盖率": ratio * 100.0,
            }
        )

    if compact:
        done_n = len(rows_for_df)
        total_n = len(report)
        if done_n:
            avg_pct = sum(r["覆盖率"] for r in rows_for_df) / done_n
            expander_label = (
                f"📊 章节覆盖率（摘要 · {done_n}/{total_n} 章 · 均 {avg_pct:.1f}%）"
            )
        else:
            expander_label = "📊 章节覆盖率（摘要 · 尚无已完成章节）"
        with st.expander(expander_label, expanded=False):
            if not rows_for_df:
                st.caption("尚无已完成章节，第一章写入后将显示覆盖率。")
                return
            st.caption(
                f"已完成 **{done_n}/{total_n}** 章 · "
                f"已完成章平均覆盖率 **{avg_pct:.1f}%**（去标点字数对比）"
            )
            st.dataframe(
                pd.DataFrame(rows_for_df),
                use_container_width=True,
                hide_index=True,
                height=min(38 + 35 * done_n, 320),
                column_config={
                    "覆盖率": st.column_config.ProgressColumn(
                        "覆盖率",
                        format="%.1f%%",
                        min_value=0,
                        max_value=100,
                    ),
                },
            )
        return

    done_with_lines = sum(
        1 for row in report if line_counts.get(int(row["chapter_num"]), 0) > 0
    )
    over_pct = sum(
        1
        for row in report
        if line_counts.get(int(row["chapter_num"]), 0) > 0
        and float(row["ratio"]) > 1.02
    )
    expander_label = (
        f"📊 章节覆盖率（全书 {len(report)} 章 · 已有剧本 {done_with_lines} 章"
        + (f" · {over_pct} 章>100%" if over_pct else "")
        + "）"
    )

    full_rows = []
    for row in report:
        ch = int(row["chapter_num"])
        ratio = float(row["ratio"])
        full_rows.append(
            {
                "章": ch,
                "剧本行数": line_counts.get(ch, 0),
                "原文(去标点)": int(row["source_no_punct"]),
                "剧本(去标点)": int(row["script_no_punct"]),
                "原文(含标点)": int(row["source_raw"]),
                "剧本(含标点)": int(row["script_raw"]),
                "覆盖率": ratio * 100.0,
            }
        )

    with st.expander(expander_label, expanded=False):
        st.caption(
            "对比方式：原文章节正文 vs 库内该章全部剧本行 `content` 拼接；"
            "均剔除空白与标点（中英文）后计字数。覆盖率 = 剧本字数 ÷ 原文字数。"
        )

        st.dataframe(
            pd.DataFrame(full_rows),
            use_container_width=True,
            hide_index=True,
            column_config={
                "覆盖率": st.column_config.ProgressColumn(
                    "覆盖率",
                    format="%.1f%%",
                    min_value=0,
                    max_value=100,
                ),
            },
        )

        with get_connection() as conn:
            all_script = conn.execute(
                "SELECT content FROM script_lines ORDER BY chapter_num, line_idx"
            ).fetchall()
        all_script_text = "".join(str(r[0] or "") for r in all_script)
        book_stats = compare_text_coverage(novel_text, all_script_text)
        overall_pct = float(book_stats["ratio"]) * 100.0
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("全书覆盖率(去标点)", f"{overall_pct:.1f}%")
        with c2:
            st.metric("原文字数(去标点)", f"{int(book_stats['source_no_punct']):,}")
        with c3:
            st.metric("剧本字数(去标点)", f"{int(book_stats['script_no_punct']):,}")

        if st.checkbox("显示各章进度条", value=False, key="coverage_show_progress_bars"):
            for row in report:
                num = int(row["chapter_num"])
                ratio = float(row["ratio"])
                color = _coverage_status_color(ratio)
                st.markdown(
                    f"<span style='color:{color};font-weight:600'>第 {num} 章</span> "
                    f"{ratio * 100:.1f}% · "
                    f"剧本 {int(row['script_no_punct']):,} / "
                    f"原文 {int(row['source_no_punct']):,}（去标点）",
                    unsafe_allow_html=True,
                )
                st.progress(min(1.0, ratio))

    _render_chapter_reprocess_and_duplicate_panel(novel_text, report, line_counts)


def _render_chapter_reprocess_and_duplicate_panel(
    novel_text: str,
    report: list[dict],
    line_counts: dict[int, int],
) -> None:
    """指定章节重跑 + 重复剧本行检测。"""
    over_chapters = [
        int(row["chapter_num"])
        for row in report
        if line_counts.get(int(row["chapter_num"]), 0) > 0
        and float(row["ratio"]) > CHAPTER_COVERAGE_MAX
    ]
    low_chapters = [
        int(row["chapter_num"])
        for row in report
        if line_counts.get(int(row["chapter_num"]), 0) > 0
        and float(row["ratio"]) < CHAPTER_COVERAGE_MIN
    ]
    issue_chapters = sorted(set(over_chapters) | set(low_chapters))
    with st.expander(
        "🔁 指定章节重新拆解 / 重复检测",
        expanded=bool(issue_chapters),
    ):
        st.caption(
            f"覆盖率目标 **{CHAPTER_COVERAGE_MIN:.0%}–{CHAPTER_COVERAGE_MAX:.1%}**（去标点字数比）。"
            f"**>{CHAPTER_COVERAGE_MAX:.1%}** 多为重复行或扩写；"
            f"**<{CHAPTER_COVERAGE_MIN:.0%}** 多为模型漏句，建议重跑该章。"
            "每章重跑前会删除该章旧剧本行；若检查点已标记完成，须在此显式选中重跑。"
        )
        if low_chapters:
            low_detail = "、".join(
                f"第{n}章({float(next(r['ratio'] for r in report if int(r['chapter_num'])==n))*100:.1f}%)"
                for n in low_chapters[:12]
            )
            st.error(
                f"以下章节覆盖率偏低（<{CHAPTER_COVERAGE_MIN:.0%}），可能丢句，建议重跑：**{low_detail}**"
                + ("…" if len(low_chapters) > 12 else "")
            )
        if over_chapters:
            st.warning(
                f"以下章节覆盖率偏高（>{CHAPTER_COVERAGE_MAX:.1%}），建议检测重复后重跑：**"
                f"{'、'.join(f'第{n}章' for n in over_chapters[:12])}**"
                + ("…" if len(over_chapters) > 12 else "")
            )

        chapter_options = sorted(
            {
                int(row["chapter_num"])
                for row in report
                if line_counts.get(int(row["chapter_num"]), 0) > 0
            }
        )
        if not chapter_options:
            st.info("尚无已写入剧本的章节。")
            return

        default_pick = list(
            dict.fromkeys([*low_chapters, *over_chapters[:3]])
        )[:6]
        picked = st.multiselect(
            "选择要重新拆解的章节（按书中章节号）",
            options=chapter_options,
            default=[c for c in default_pick if c in chapter_options],
            format_func=lambda n: f"第 {n} 章",
            key="reprocess_chapter_multiselect",
        )

        if picked:
            picked_sorted = sorted(int(c) for c in picked)
            with get_connection() as conn:
                for audit_ch in picked_sorted:
                    dup = audit_chapter_script_duplicates(conn, audit_ch)
                    st.markdown(
                        f"**第 {audit_ch} 章**：{dup['line_count']} 行 · "
                        f"估计重复字数占比 **{dup['duplicate_ratio'] * 100:.1f}%** · "
                        f"重复组 **{dup['duplicate_group_count']}** 个"
                    )
                    if dup["groups"]:
                        for g in dup["groups"][:8]:
                            st.markdown(
                                f"- 行 {g['line_idxs']} 重复 **{g['repeat_count']}** 次 · "
                                f"「{g['preview']}」"
                            )
                    elif dup["line_count"]:
                        st.caption(
                            "未发现较长内容的完全重复行（>100% 可能来自模型改写扩写）。"
                        )
                    if dup["consecutive_duplicate_line_idxs"]:
                        st.caption(
                            "相邻重复行号："
                            + "、".join(
                                str(i)
                                for i in dup["consecutive_duplicate_line_idxs"][:20]
                            )
                        )
                    if audit_ch != picked_sorted[-1]:
                        st.divider()

        pipeline_busy = bool(st.session_state.get("pipeline_running"))
        if pipeline_busy:
            st.caption("拆解任务运行中，请等待当前任务结束后再发起新的拆解或重跑。")
        if st.button(
            "🔁 重新拆解选中章节",
            type="primary",
            disabled=not picked or pipeline_busy,
            key="reprocess_chapters_btn",
        ):
            st.session_state.trigger_reprocess_chapters = [int(c) for c in picked]
            st.rerun()


def render_blocked_segments_and_manual_edit(
    *,
    key_suffix: str = "",
    chapter_filter: int | None = None,
) -> None:
    """待手动录入的敏感段落 + 剧本/演员表编辑。"""
    fp = (st.session_state.get("novel_fingerprint") or "").strip()
    if not fp:
        return

    with get_connection() as conn:
        pending_n = count_pending_blocked_segments(conn, fp)
        if pending_n == 0:
            return
        segments = list_pending_blocked_segments(
            conn, fp, chapter_num=chapter_filter
        )

    st.warning(
        f"有 **{pending_n}** 段原文未能自动拆解（审核拦截），"
        "请根据下方摘录手动录入剧本行后点「确认本段已处理」。"
        + (
            f"（当前筛选：第 {chapter_filter} 章 · 本页 {len(segments)} 段）"
            if chapter_filter is not None
            else ""
        )
    )

    for seg in segments:
        seg_id = int(seg["id"])
        ch = int(seg["chapter_num"])
        label = f"第 {ch} 章 · 待录入 #{seg_id}（{seg['char_end'] - seg['char_start']} 字）"
        with st.expander(label, expanded=(chapter_filter == ch)):
            st.text_area(
                "无法自动生成的原文摘录",
                value=str(seg.get("snippet") or ""),
                height=120,
                disabled=True,
                key=f"blocked_snippet_{seg_id}{key_suffix}",
            )
            st.caption(f"原因：{seg.get('reason') or 'censorship_blocked'}")

            with get_connection() as conn:
                existing = fetch_script_lines_for_edit(conn, ch)
            max_idx = max((int(r["line_idx"]) for r in existing), default=0)

            c1, c2, c3 = st.columns([1, 1, 2])
            with c1:
                after_idx = st.number_input(
                    "插入于行号之后（0=章首）",
                    min_value=0,
                    max_value=max(max_idx, 0),
                    value=max_idx,
                    key=f"blocked_after_{seg_id}{key_suffix}",
                )
            with c2:
                role = st.text_input(
                    "角色",
                    value="旁白",
                    key=f"blocked_role_{seg_id}{key_suffix}",
                )
            with c3:
                is_dlg = st.checkbox(
                    "对白",
                    value=False,
                    key=f"blocked_dlg_{seg_id}{key_suffix}",
                )
            content = st.text_area(
                "剧本正文",
                height=100,
                key=f"blocked_content_{seg_id}{key_suffix}",
            )
            emotion = st.text_input(
                "语气指令（可选）",
                key=f"blocked_emotion_{seg_id}{key_suffix}",
            )
            if st.button(
                "➕ 插入一行到剧本",
                key=f"blocked_insert_{seg_id}{key_suffix}",
                type="primary",
            ):
                if not (content or "").strip():
                    st.error("请先填写剧本正文。")
                else:
                    with get_connection() as conn:
                        insert_script_line_manual(
                            conn,
                            chapter_num=ch,
                            after_line_idx=int(after_idx),
                            role=role,
                            content=content.strip(),
                            is_dialogue=is_dlg,
                            emotion_instruction=emotion,
                        )
                        conn.commit()
                    st.success(f"已插入第 {ch} 章，行号已自动重排。")
                    st.rerun()

            if st.button(
                "✅ 确认本段已处理",
                key=f"blocked_resolve_{seg_id}{key_suffix}",
            ):
                with get_connection() as conn:
                    resolve_blocked_segment(conn, seg_id)
                    conn.commit()
                st.success("已标记为已处理。")
                st.rerun()


def _save_script_editor_rows(
    conn,
    chapter_num: int,
    rows: list[dict],
    edited: pd.DataFrame,
) -> None:
    orig_by_id = {int(r["id"]): r for r in rows}
    for _, row in edited.iterrows():
        lid = int(row["id"])
        orig = orig_by_id.get(lid)
        if not orig:
            continue
        fields: dict = {}
        for field in (
            "line_idx",
            "role",
            "content",
            "emotion_instruction",
            "voice_id",
            "is_dialogue",
        ):
            if field not in row:
                continue
            new_val = row[field]
            old_val = orig.get(field)
            if field == "is_dialogue":
                new_val = bool(new_val)
                old_val = bool(old_val)
            if new_val != old_val:
                fields[field] = new_val
        if fields:
            update_script_line_by_id(conn, lid, fields)
    renumber_chapter_line_idx(conn, chapter_num)


def render_merged_script_chapter_panel(
    chapter_num: int,
    *,
    key_suffix: str = "",
) -> None:
    """单章剧本：预览与编辑合一（data_editor）。"""
    with get_connection() as conn:
        rows = fetch_script_lines_for_edit(conn, chapter_num)

    label = f"第 {chapter_num} 章剧本（{len(rows)} 行 · 可查看/编辑）"
    with st.expander(label, expanded=True):
        st.caption(
            "表格中 **行号** 即章内顺序（第 1、2、3… 行）；**id** 为数据库主键（删除时请填行号，不要填 id）。"
        )
        if not rows:
            st.caption("本章暂无剧本行，可在上方「待录入」区插入，或点击下方插入首行。")
            if st.button(
                "➕ 插入第 1 行（空白）",
                key=f"script_insert_first_{chapter_num}{key_suffix}",
            ):
                with get_connection() as conn:
                    insert_blank_script_line_at(
                        conn,
                        chapter_num=chapter_num,
                        line_idx=1,
                        position="above",
                    )
                    conn.commit()
                st.success("已插入空白行。")
                st.rerun()
            return

        export_name = _script_export_filename(chapter_num)
        with get_connection() as conn:
            export_bytes = export_offline_csv_bytes(conn, chapter_num=chapter_num)
        st.download_button(
            label="⬇️ 导出本章 CSV（含演员表）",
            data=export_bytes,
            file_name=export_name,
            mime="text/csv",
            key=f"script_csv_{export_name}{key_suffix}",
        )

        df = pd.DataFrame(rows)[
            [
                "id",
                "line_idx",
                "role",
                "content",
                "is_dialogue",
                "emotion_instruction",
                "voice_id",
            ]
        ]
        edited = st.data_editor(
            df,
            num_rows="fixed",
            use_container_width=True,
            hide_index=True,
            column_config={
                "id": st.column_config.NumberColumn("id（库内）", disabled=True),
                "line_idx": st.column_config.NumberColumn("行号"),
                "role": st.column_config.TextColumn("角色"),
                "content": st.column_config.TextColumn("正文", width="large"),
                "is_dialogue": st.column_config.CheckboxColumn("对白"),
                "emotion_instruction": st.column_config.TextColumn("语气指令"),
                "voice_id": st.column_config.TextColumn("音色ID"),
            },
            key=f"script_editor_{chapter_num}{key_suffix}",
        )

        if st.button(
            "💾 保存修改",
            key=f"script_editor_save_{chapter_num}{key_suffix}",
        ):
            with get_connection() as conn:
                _save_script_editor_rows(conn, chapter_num, rows, edited)
                conn.commit()
            st.success("剧本已保存，行号已重排。")
            st.rerun()

        st.markdown("**插入 / 删除行**（按表格「行号」列，不是 id 列）")
        max_line = max(int(r["line_idx"]) for r in rows)
        ins_col1, ins_col2, ins_col3 = st.columns([1, 1, 1])
        with ins_col1:
            op_line_idx = st.number_input(
                "目标行号",
                min_value=1,
                max_value=max_line,
                value=1,
                step=1,
                key=f"script_op_line_idx_{chapter_num}{key_suffix}",
            )
        with ins_col2:
            insert_position = st.radio(
                "插入位置",
                options=["上方", "下方"],
                horizontal=True,
                key=f"script_insert_pos_{chapter_num}{key_suffix}",
            )
        with ins_col3:
            st.write("")
            st.write("")
            if st.button(
                "➕ 插入空白行",
                key=f"script_insert_blank_{chapter_num}{key_suffix}",
                use_container_width=True,
            ):
                pos_key = "above" if insert_position == "上方" else "below"
                with get_connection() as conn:
                    new_idx = insert_blank_script_line_at(
                        conn,
                        chapter_num=chapter_num,
                        line_idx=int(op_line_idx),
                        position=pos_key,
                    )
                    conn.commit()
                st.success(
                    f"已在第 {op_line_idx} 行{insert_position}插入空白行（新行号 {new_idx}）。"
                )
                st.rerun()

        preview = next(
            (r for r in rows if int(r["line_idx"]) == int(op_line_idx)),
            None,
        )
        if preview:
            prev_text = str(preview.get("content") or "")[:120]
            st.caption(
                f"第 {op_line_idx} 行当前内容：{prev_text}"
                f"{'…' if len(str(preview.get('content') or '')) > 120 else ''}"
            )

        if st.button(
            "🗑️ 删除目标行",
            key=f"script_del_btn_{chapter_num}{key_suffix}",
        ):
            with get_connection() as conn:
                ch = delete_script_line_by_chapter_line_idx(
                    conn, chapter_num, int(op_line_idx)
                )
                conn.commit()
            if ch is None:
                st.error(f"第 {chapter_num} 章没有行号 {op_line_idx}。")
            else:
                st.success(
                    f"已删除第 {chapter_num} 章第 {op_line_idx} 行并重排后续行号。"
                )
                st.rerun()


def render_merged_character_panel(
    char_rows: list[dict],
    *,
    key_suffix: str = "",
    editable: bool = True,
) -> None:
    """演员表：预览与编辑合一。"""
    if not char_rows:
        return
    edit_cols = [
        "name",
        "dialogue_lines",
        "gender",
        "age",
        "personality",
        "quote_1",
        "quote_2",
        "quote_1_instruction",
        "quote_2_instruction",
        "voice_id",
    ]
    df = pd.DataFrame(char_rows)[[c for c in edit_cols if c in char_rows[0]]]
    view_only = not editable
    expander_title = (
        f"演员表（共 {len(char_rows)} 人 · 只读预览）"
        if view_only
        else f"演员表（共 {len(char_rows)} 人 · 可查看/编辑）"
    )
    with st.expander(expander_title, expanded=False):
        st.caption(
            "排序：按 **对白行数** 从高到低；「对白行数」为只读统计。"
            + ("" if view_only else " 可在下方直接修改并保存。")
        )
        column_config = {
            "name": st.column_config.TextColumn("角色", disabled=True),
            "dialogue_lines": st.column_config.NumberColumn(
                "对白行数", disabled=True
            ),
            "gender": st.column_config.TextColumn("性别", disabled=view_only),
            "age": st.column_config.TextColumn("年龄", disabled=view_only),
            "personality": st.column_config.TextColumn(
                "人设", width="large", disabled=view_only
            ),
            "quote_1": st.column_config.TextColumn(
                "代表台词1", disabled=view_only
            ),
            "quote_2": st.column_config.TextColumn(
                "代表台词2", disabled=view_only
            ),
            "quote_1_instruction": st.column_config.TextColumn(
                "台词1情绪", disabled=view_only
            ),
            "quote_2_instruction": st.column_config.TextColumn(
                "台词2情绪", disabled=view_only
            ),
            "voice_id": st.column_config.TextColumn(
                "音色ID", disabled=view_only
            ),
        }
        if view_only:
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            edited = st.data_editor(
                df,
                num_rows="fixed",
                use_container_width=True,
                hide_index=True,
                column_config=column_config,
                key=f"char_editor{key_suffix}",
            )
            if st.button(
                "💾 保存演员表修改", key=f"char_editor_save{key_suffix}"
            ):
                orig_by_name = {str(r["name"]): r for r in char_rows}
                with get_connection() as conn:
                    for _, row in edited.iterrows():
                        name = str(row["name"])
                        orig = orig_by_name.get(name)
                        if not orig:
                            continue
                        fields = {}
                        for field in edit_cols:
                            if field in ("name", "dialogue_lines"):
                                continue
                            if field not in row:
                                continue
                            if row[field] != orig.get(field):
                                fields[field] = row[field]
                        if fields:
                            update_character_by_name(conn, name, fields)
                    conn.commit()
                st.success("演员表已保存。")
                st.rerun()


def render_script_debug_panel(
    novel_text: str = "",
    *,
    key_suffix: str = "",
    run_maintenance: bool = True,
    show_coverage: bool = True,
    coverage_compact: bool = False,
    show_enrich: bool = True,
    live_badge: bool = False,
    characters_editable: bool = True,
) -> None:
    """Show partial script/character data from SQLite."""
    pruned = 0
    if run_maintenance:
        with get_connection() as conn:
            merged = compact_fragmented_script_lines(conn)
            pruned = prune_spurious_characters(conn)
            synced_roles = sync_speaking_roles_to_cast(conn)
            from utils.role_voice import sync_orphan_script_role_voices

            role_voices = sync_orphan_script_role_voices(conn)
            if merged or pruned or synced_roles or any(role_voices.values()):
                conn.commit()
    with get_connection() as conn:
        stats = get_pipeline_stats(conn)
        chapters = list_script_chapters(conn)
    if stats["script_lines"] == 0 and stats["characters"] == 0:
        return

    title = "**📝 本地剧本库预览**"
    if live_badge:
        title += " · <span style='color:#2563eb;font-size:0.9em'>拆解中实时更新</span>"
    st.markdown(title, unsafe_allow_html=live_badge)
    st.caption(
        f"当前库内：**{stats['characters']}** 个角色 · "
        f"**{stats['script_lines']}** 条剧本行 · "
        f"**{stats['chapters']}** 章"
        + (f" · 已清理误识别演员 {pruned} 条" if pruned else "")
    )

    if show_coverage and novel_text.strip():
        render_chapter_coverage_panel(novel_text, compact=coverage_compact)

    with get_connection() as conn:
        incomplete = list_incomplete_character_names(conn)
        char_rows = fetch_characters_preview(conn)
        overlong = [
            r["name"]
            for r in char_rows
            if len(str(r.get("personality") or "")) > PERSONALITY_MAX_CHARS
        ]

    if show_enrich and overlong:
        st.info(
            f"以下演员人设超过 {PERSONALITY_MAX_CHARS} 字：**{'、'.join(overlong[:12])}**"
            + ("…" if len(overlong) > 12 else "")
            + "。可点击下方按钮自动压缩提炼。"
        )
        api_key = st.session_state.get("step_api_key", "").strip()
        if api_key and st.button(
            f"✂️ 压缩超长人设（≤{PERSONALITY_MAX_CHARS} 字）",
            key=f"condense_personality_btn{key_suffix}",
            type="secondary",
        ):
            with st.spinner("正在调用模型压缩人设…"):
                with get_connection() as conn:
                    try:
                        condense_overlong_personalities(conn, api_key)
                        conn.commit()
                        st.success("超长人设已压缩，请查看演员表。")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"压缩失败：{exc}")
        elif not api_key:
            st.caption("压缩人设需先在侧边栏配置 Step API Key。")

    if show_enrich and incomplete:
        st.warning(
            f"以下演员缺少人设字段（性别/性格等）：**{'、'.join(incomplete)}**。"
            "可点击下方按钮自动补全，或重新拆解对应章节。"
        )
        api_key = st.session_state.get("step_api_key", "").strip()
        if api_key and st.button(
            "✨ 补全空缺演员人设",
            key=f"enrich_characters_btn{key_suffix}",
            type="secondary",
        ):
            with st.spinner("正在调用模型补全演员档案…"):
                with get_connection() as conn:
                    try:
                        enrich_incomplete_characters(conn, api_key)
                        conn.commit()
                        st.success("演员人设已更新，请查看下方演员表。")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"补全失败：{exc}")
        elif not api_key:
            st.caption("补全人设需先在侧边栏配置 Step API Key。")

    if char_rows:
        render_merged_character_panel(
            char_rows,
            key_suffix=key_suffix,
            editable=characters_editable and not live_badge,
        )

    chapter_options = ["全部章节"] + [f"第 {n} 章" for n in chapters]
    selected_chapter = st.selectbox(
        "剧本行 · 按章节筛选",
        chapter_options,
        help="预览特定章节时，可以进一步编辑",
        key=f"script_preview_chapter_filter{key_suffix}",
    )
    chapter_filter: int | None = None
    if selected_chapter != "全部章节":
        digits = "".join(ch for ch in selected_chapter if ch.isdigit())
        chapter_filter = int(digits) if digits else None

    render_blocked_segments_and_manual_edit(
        key_suffix=key_suffix,
        chapter_filter=chapter_filter,
    )
    if novel_text.strip() and chapter_filter is not None:
        with get_connection() as conn:
            ch_report = compute_novel_chapter_coverage_report(novel_text, conn)
        ch_row = next(
            (r for r in ch_report if int(r["chapter_num"]) == chapter_filter),
            None,
        )
        if ch_row is not None:
            ratio = float(ch_row["ratio"])
            st.caption(
                f"第 {chapter_filter} 章覆盖率（去标点）：**{ratio * 100:.1f}%** · "
                f"剧本 {int(ch_row['script_no_punct']):,} / "
                f"原文 {int(ch_row['source_no_punct']):,} 字"
                + (" · 已超过 100%（可能有重复行）" if ratio > 1.0 else "")
            )

    if chapter_filter is not None:
        render_merged_script_chapter_panel(
            chapter_filter, key_suffix=key_suffix
        )
    else:
        with get_connection() as conn:
            script_rows = fetch_script_lines_preview(conn, chapter_num=None)
        if script_rows:
            with st.expander(
                f"剧本行（全部 · {len(script_rows)} 条 · 只读）",
                expanded=False,
            ):
                st.caption("选择某一章后可在此区域编辑、删除行。")
                export_name = _script_export_filename(None)
                _render_script_csv_export_import(
                    export_name=export_name,
                    key_suffix=key_suffix,
                )
                _render_scrollable_html_table(
                    _dataframe_chinese_columns(script_rows, SCRIPT_TABLE_HEADERS),
                    _wrap_columns_for_headers(
                        frozenset({"content", "emotion_instruction"}),
                        SCRIPT_TABLE_HEADERS,
                    ),
                )


def render_local_book_progress_banner(progress) -> None:
    """提示本地是否已有与当前文件一致的拆解半成品。"""
    if not progress.has_local_data:
        return

    gap_line = ""
    if progress.chapter_gap_hint:
        gap_line = f"\n- {progress.chapter_gap_hint}"

    if progress.offline_script_ready:
        st.success(
            f"**本地已有离线剧本**（已导入或手工维护，无 API 拆解断点）\n\n"
            f"- 库内 **{progress.script_lines}** 条剧本行 · "
            f"**{progress.characters}** 个角色 · "
            f"**{progress.chapters_in_db}** 章有数据"
            f"{gap_line}\n"
            f"- 可直接 **试镜、录制**；无需断点续跑。"
            "若要对当前 TXT 重新自动拆解，请使用「启动全书拆解」（会清空现有剧本）。"
        )
        return

    if progress.can_resume:
        next_ch = progress.next_chunk_index or "—"
        st.success(
            f"**检测到本书本地半成品（与当前上传内容一致）**\n\n"
            f"- 已完成 **{progress.completed_chunks}/{progress.total_chunks}** 个拆解任务\n"
            f"- 库内 **{progress.script_lines}** 条剧本行 · "
            f"**{progress.characters}** 个角色 · "
            f"**{progress.chapters_in_db}** 章有数据"
            f"{gap_line}\n"
            f"- 建议直接点 **「▶️ 断点续跑」**，从下一未完成任务（任务序 **{next_ch}**）继续，"
            f"无需再点「启动全书拆解」（该按钮会**永久删除**本地已有剧本）。"
        )
        return

    if progress.is_complete:
        st.info(
            f"本书本地拆解已全部完成（**{progress.completed_chunks}/"
            f"{progress.total_chunks}** 任务）。"
            f"库内 **{progress.script_lines}** 条剧本行。"
            f"{gap_line} "
            "若要全书重跑，请使用下方「启动全书拆解」。"
        )
        return

    if progress.resume_block_reason:
        st.warning(progress.resume_block_reason)
        return

    st.warning(
        "本地库内有剧本数据，但与当前文件的断点检查点不一致或已损坏。"
        f"{gap_line} "
        "若刚更换了章节切分规则，请从头拆解；否则可尝试断点续跑或清空后重跑。"
    )


def render_chapter_integrity_panel(report: dict) -> None:
    """拆解前展示章节分布与将执行的拆解顺序。"""
    mode = report.get("slice_mode") or ""
    if mode == "empty":
        st.warning("无法检查章节：正文为空。")
        return

    if mode == "fixed":
        st.info(
            f"未检测到「第N章」标题，将按 **{FALLBACK_CHAPTER_SIZE}** 字/段切分，"
            f"共 **{report.get('process_count', 0)}** 段（与章节号无关）。"
        )
        return

    ok = bool(report.get("ok"))
    process_count = int(report.get("process_count") or 0)
    unique_n = int(report.get("unique_marker_count") or 0)
    marker_n = int(report.get("marker_count") or 0)

    with st.expander("📑 章节分布检查（拆解前建议查看）", expanded=not ok):
        if ok:
            st.success(
                f"章节分布检查通过：文中 **{unique_n}** 个不同章节号，"
                f"将按 **{process_count}** 次任务顺序拆解（第 1 章至第 {report.get('max_marker', 1)} 章连续无缺号）。"
            )
        else:
            st.error(
                "检测到章节 **重复或缺号**，拆解顺序可能与书名章节不一致。"
                "建议先修正 TXT 再拆解，或勾选下方确认后继续。"
            )
            for issue in report.get("issues") or []:
                st.markdown(f"- {issue}")

        seq = report.get("process_sequence") or []
        if seq:
            preview = " → ".join(f"第{n}章" for n in seq[:24])
            if len(seq) > 24:
                preview += f" …（共 {len(seq)} 个任务）"
            st.caption(f"**实际拆解顺序**：{preview}")

        catalog = report.get("chapter_catalog") or []
        if catalog:
            st.markdown("**章节目录（去重，便于核对/导出）**")
            catalog_rows = [
                {
                    "章节号": c["chapter_num"],
                    "章节开头": c.get("chapter_title") or "—",
                    "行号": c["line"],
                    "标记原文": c["label"],
                }
                for c in catalog
            ]
            st.dataframe(
                pd.DataFrame(catalog_rows),
                use_container_width=True,
                hide_index=True,
                height=min(38 + 35 * min(len(catalog_rows), 14), 480),
            )

        hits = report.get("marker_hits") or []
        if hits:
            st.markdown("**文中「第N章」标题一览**（按出现顺序，含重复）")
            rows = [
                {
                    "序号": i + 1,
                    "章节号": h["chapter_num"],
                    "章节开头": h.get("chapter_title") or "—",
                    "行号": h["line"],
                    "标记原文": h["label"],
                }
                for i, h in enumerate(hits)
            ]
            st.dataframe(
                pd.DataFrame(rows),
                use_container_width=True,
                hide_index=True,
                height=min(38 + 35 * min(len(rows), 12), 420),
            )

        plan = report.get("slice_plan") or []
        if plan:
            st.markdown("**拆解切分计划**（将按此顺序调用模型）")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "任务序": p["pipeline_index"],
                            "章节号": p["chapter_num"],
                            "章节开头": p.get("chapter_title") or "—",
                            "字数": p["chars"],
                            "说明": p.get("note", ""),
                        }
                        for p in plan
                    ]
                ),
                use_container_width=True,
                hide_index=True,
                height=min(38 + 35 * min(len(plan), 12), 420),
            )


def _render_script_csv_export_import(
    *,
    export_name: str | None = None,
    chapter_num: int | None = None,
    key_suffix: str = "",
) -> None:
    """离线剧本+演员表 CSV 导出与导入（与拆解区、剧本预览共用）。"""
    with get_connection() as conn:
        script_n = int(
            conn.execute("SELECT COUNT(*) FROM script_lines").fetchone()[0]
        )
        cast_n = int(conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0])
        can_export = script_n > 0 or cast_n > 0

    col_export, col_import = st.columns([1, 1])
    with col_export:
        if can_export:
            name = export_name or _script_export_filename(chapter_num)
            with get_connection() as conn:
                export_bytes = export_offline_csv_bytes(
                    conn, chapter_num=chapter_num
                )
            st.download_button(
                label="⬇️ 导出剧本 CSV（含演员表）",
                data=export_bytes,
                file_name=name,
                mime="text/csv",
                key=f"script_csv_dl_{name}{key_suffix}",
            )
            st.caption(
                "CSV 含「记录类型」列：`剧本` 为台词行，`演员` 为人设与音色；"
                "导出时始终附带全书演员表。"
            )
        else:
            st.caption("库内尚无剧本或演员数据，导出不可用。")

    with col_import:
        uploaded = st.file_uploader(
            "导入离线剧本 CSV",
            type=["csv"],
            key=f"script_csv_import_file{key_suffix}",
            help=(
                "支持新版（含「记录类型」+ 演员行）与旧版纯剧本 CSV。"
                "有演员行时将保留人设、试镜台词与音色，不再按角色名重建空表。"
            ),
        )
        replace_existing = st.checkbox(
            "导入前清空现有剧本与演员表",
            value=True,
            key=f"script_csv_import_replace{key_suffix}",
        )
        if st.button(
            "📥 导入剧本 CSV",
            key=f"script_csv_import_btn{key_suffix}",
            disabled=uploaded is None,
            help="写入本地剧本库；若已上传本书，将同时清除本书拆解断点。",
        ):
            fp = (st.session_state.get("novel_fingerprint") or "").strip()
            try:
                stats = import_script_csv_bytes(
                    uploaded.getvalue(),
                    replace_existing=replace_existing,
                    novel_fingerprint=fp or None,
                )
                cast_msg = (
                    f" · **{stats.get('cast', 0)}** 条演员表"
                    if stats.get("cast")
                    else ""
                )
                st.success(
                    f"已导入 **{stats['lines']}** 条剧本行（"
                    f"**{stats['chapters']}** 章 · **{stats['roles']}** 个角色"
                    f"{cast_msg}）。可在下方试镜与录制区继续。"
                )
                st.rerun()
            except ScriptCsvImportError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"导入失败：{exc}")


def render_pipeline_section() -> None:
    """Script breakdown button, progress, and result summary."""
    global _pipeline_worker_thread

    st.subheader("⚙️ 剧本智能拆解")

    with st.expander("📁 离线剧本 CSV", expanded=False):
        st.caption(
            "导出后在表格软件中修改剧本行与演员行，再导入写回本地库。"
            "导出文件含「记录类型」：`剧本` / `演员`；导入后可直接试镜与录制。"
        )
        _render_script_csv_export_import(key_suffix="_pipeline")

    novel_text = st.session_state.get("uploaded_novel_text", "")
    if not novel_text:
        st.info("请先上传小说 .txt 文件后再启动拆解。")
        return

    if st.session_state.get("pipeline_log_text"):
        log_lines = st.session_state.pipeline_log_text.count("\n") + 1
        with st.expander(
            f"📋 最近一次运行日志（{log_lines} 行）",
            expanded=False,
        ):
            _render_scrollable_log_body(st.session_state.pipeline_log_text)

    novel_name = st.session_state.get("uploaded_novel_name", "")
    local_progress = get_local_book_progress(novel_text, novel_name)
    render_local_book_progress_banner(local_progress)

    chapter_integrity = audit_novel_chapter_integrity(novel_text)
    render_chapter_integrity_panel(chapter_integrity)
    integrity_ok = bool(chapter_integrity.get("ok"))
    if chapter_integrity.get("slice_mode") == "marker" and not integrity_ok:
        st.checkbox(
            "我已了解章节重复/缺号等问题，仍按当前切分继续拆解",
            key="chapter_integrity_ack",
        )

    pipeline_busy = _sync_pipeline_busy_state()
    if pipeline_busy:
        st.warning(
            "已有拆解任务在后台运行（断点续跑 / 指定章节重跑 / 全书拆解）。"
            "请等待其结束或刷新页面查看进度，**勿重复点击**，否则可能多任务并发写库。"
        )
        return

    if _pipeline_worker_alive():
        st.error(
            "检测到上一次拆解线程仍在后台（可能页面刷新后状态不同步）。"
            "请等待其结束或完全退出应用后再启动，避免并发写库。"
        )
        return

    col_restart, col_resume = st.columns(2)
    resume_primary = local_progress.can_resume
    if resume_primary:
        col_resume.caption(
            "推荐：保留已有剧本与演员表；仅跳过已完整跑完的章，"
            "上次中断中的那一章会重新调 API。"
        )
        col_restart.caption(
            "将**永久删除**本地全部剧本与断点（含已生成的第 2、3、4 章等），"
            "仅在新书或确认全书重跑时使用。"
        )
    resume_help = "跳过已完成章节，从下一任务继续（不清空库）。"
    if local_progress.resume_block_reason:
        resume_help = local_progress.resume_block_reason
    start_resume = col_resume.button(
        "▶️ 断点续跑",
        type="primary" if resume_primary else "secondary",
        use_container_width=True,
        disabled=not local_progress.can_resume,
        help=resume_help,
    )
    start_restart = col_restart.button(
        "⚙️ 启动全书拆解",
        type="secondary" if resume_primary else "primary",
        use_container_width=True,
        help=f"清空本地 SQLite 后按章流式拆解，每章写入后自动 Top {ROLLING_RANK_TOP_N} 演员榜重排。",
    )

    pending_reprocess = st.session_state.pop("trigger_reprocess_chapters", None)

    if not (start_restart or start_resume or pending_reprocess):
        st.session_state.pipeline_running = False
        return

    if not st.session_state.legal_agreed:
        st.error("请先勾选法律免责声明。")
        return
    api_key = st.session_state.get("step_api_key", "").strip()
    if not api_key:
        st.error("请先在侧边栏配置 Step API Key。")
        return

    if (
        chapter_integrity.get("slice_mode") == "marker"
        and not integrity_ok
        and not st.session_state.get("chapter_integrity_ack")
    ):
        st.error(
            "章节分布检查未通过：请展开「章节分布检查」核对重复/缺号，"
            "或勾选「仍按当前切分继续拆解」后再启动。"
        )
        return

    do_resume = start_resume and not start_restart and not pending_reprocess
    reprocess_chapters = (
        [int(c) for c in pending_reprocess] if pending_reprocess else None
    )

    st.session_state.pipeline_running = True

    novel_text_snapshot = novel_text
    api_key_snapshot = api_key
    novel_name_snapshot = novel_name

    total_steps = (
        len(reprocess_chapters)
        if reprocess_chapters
        else estimate_chunk_count(novel_text_snapshot)
    )
    st.session_state.pipeline_log_lines = []
    progress_bar = st.progress(0.0)
    status_box = st.empty()
    if reprocess_chapters:
        status_box.markdown(
            f"**指定章节重跑** · "
            f"{'、'.join(f'第 {c} 章' for c in reprocess_chapters)}"
        )
    with st.expander("📋 运行日志（实时）", expanded=False):
        log_box = st.empty()
    preview_live = st.empty()

    def refresh_log_panel() -> None:
        _render_pipeline_log_box(log_box, st.session_state.pipeline_log_lines)

    def refresh_live_preview() -> None:
        with get_connection() as conn:
            stats = get_pipeline_stats(conn)
        cache_key = (
            stats["script_lines"],
            stats["characters"],
            stats["chapters"],
        )
        if cache_key == st.session_state.get("live_preview_cache_key"):
            return
        st.session_state.live_preview_cache_key = cache_key
        preview_live.empty()
        live_key = f"_live_{stats['chapters']}_{stats['script_lines']}"
        with preview_live.container():
            if stats["script_lines"] == 0 and stats["characters"] == 0:
                st.caption("⏳ 等待第一章写入完成后，将在此显示演员表与剧本预览…")
                return
            try:
                render_script_debug_panel(
                    novel_text_snapshot,
                    key_suffix=live_key,
                    run_maintenance=False,
                    show_coverage=True,
                    coverage_compact=True,
                    show_enrich=False,
                    live_badge=True,
                    characters_editable=False,
                )
            except Exception as exc:
                st.warning(
                    f"实时预览暂时不可用（{exc}），拆解仍在后台继续；"
                    "完成后可在页面下方查看完整剧本库。"
                )

    def on_log(line: str) -> None:
        st.session_state.pipeline_log_lines.append(line)
        refresh_log_panel()

    def on_progress(completed: int, total: int, message: str) -> None:
        progress_bar.progress(min(1.0, completed / total) if total else 0.0)
        step_label = (
            f"**步骤 {completed}/{total}**"
            if completed < total
            else f"**步骤 {total}/{total}**"
        )
        scope = (
            f"（共 {total} 个任务）"
            if reprocess_chapters
            else f"（全书共 {total} 章）"
        )
        status_box.markdown(f"{step_label}{scope}\n\n{message}")

    if reprocess_chapters:
        mode_label = "指定章节重跑"
    else:
        mode_label = "断点续跑" if do_resume else "从头拆解"
    if not reprocess_chapters:
        status_box.markdown(
            f"**步骤 0/{total_steps}** · {mode_label} · 全书 **{total_steps}** 章…"
        )
    refresh_log_panel()
    st.session_state.live_preview_cache_key = None
    refresh_live_preview()

    log_queue: Queue[str] = Queue()
    progress_queue: Queue[tuple[int, int, str]] = Queue()
    done_event = threading.Event()
    outcome: dict = {"result": None, "error": None}

    def thread_on_log(line: str) -> None:
        log_queue.put(line)

    def thread_on_progress(completed: int, total: int, message: str) -> None:
        progress_queue.put((completed, total, message))

    def worker() -> None:
        attempt = 0
        use_resume = do_resume
        use_reset = not do_resume and not reprocess_chapters
        while True:
            attempt += 1
            try:
                if attempt > 1:
                    thread_on_log(
                        f"[INFO] 第 {attempt} 次拆解尝试（断点续跑）…"
                    )
                outcome["result"] = process_novel_pipeline(
                    novel_text_snapshot,
                    api_key_snapshot,
                    reset_db=use_reset and attempt == 1,
                    resume=use_resume or attempt > 1,
                    reprocess_chapter_nums=reprocess_chapters,
                    novel_name=novel_name_snapshot,
                    on_progress=thread_on_progress,
                    on_log=thread_on_log,
                )
                outcome["error"] = None
                return
            except Exception as exc:
                outcome["error"] = exc
                thread_on_log(f"[ERROR] 拆解失败: {exc}")
                wait_min = PIPELINE_FAILURE_RETRY_WAIT_SEC // 60
                thread_on_log(
                    f"[INFO] {wait_min} 分钟后自动断点续跑重试（第 {attempt + 1} 次）…"
                )
                use_reset = False
                use_resume = True
                wait_total = PIPELINE_FAILURE_RETRY_WAIT_SEC
                for left in range(wait_total, 0, -60):
                    mins = max(1, (left + 59) // 60)
                    thread_on_log(
                        f"[INFO] 距下次自动重试约 {mins} 分钟…"
                    )
                    time.sleep(min(60, left))
            finally:
                if outcome["error"] is None:
                    done_event.set()

    _pipeline_worker_thread = threading.Thread(target=worker, daemon=True)
    _pipeline_worker_thread.start()
    t0 = time.time()
    last_status_ping = 0.0
    last_activity = time.time()
    log_file_offset = (
        PIPELINE_LOG_FILE.stat().st_size if PIPELINE_LOG_FILE.exists() else 0
    )
    log_seen: set[str] = set(st.session_state.pipeline_log_lines)

    def touch_activity() -> None:
        nonlocal last_activity
        last_activity = time.time()

    try:
        while (
            not done_event.is_set()
            or not log_queue.empty()
            or not progress_queue.empty()
        ):
            while True:
                try:
                    on_log(log_queue.get_nowait())
                    touch_activity()
                except Empty:
                    break
            while True:
                try:
                    c, t, m = progress_queue.get_nowait()
                    on_progress(c, t, m)
                    touch_activity()
                except Empty:
                    break
            log_file_offset, file_log_lines = _sync_pipeline_log_from_file(
                log_file_offset, log_seen
            )
            if file_log_lines:
                st.session_state.pipeline_log_lines.extend(file_log_lines)
                touch_activity()
            refresh_log_panel()
            try:
                refresh_live_preview()
            except Exception as exc:
                st.warning(
                    f"实时预览刷新跳过（{exc}），拆解仍在继续，请查看运行日志。"
                )

            if not done_event.is_set():
                elapsed = time.time() - t0
                idle = time.time() - last_activity
                if idle >= PIPELINE_UI_INACTIVITY_SEC:
                    status_box.warning(
                        f"已连续 {idle:.0f} 秒页面无新日志（阈值 "
                        f"{PIPELINE_UI_INACTIVITY_SEC // 60} 分钟）。"
                        "后台若仍在跑，`logs/pipeline.log` 会继续写入；"
                        "若长时间停在「第 1 章」且日志无「正在写入数据库」，"
                        "请刷新页面后点 **断点续跑**（勿重复「启动全书拆解」）。"
                    )
                elif idle >= PIPELINE_FAILURE_RETRY_WAIT_SEC and not done_event.is_set():
                    status_box.error(
                        f"后台已 {PIPELINE_FAILURE_RETRY_WAIT_SEC // 60} 分钟无新进展。"
                        "10 分钟自动重试仅在**抛出异常**时触发；"
                        "若线程卡死请刷新页面后 **断点续跑**。"
                    )
                elif elapsed - last_status_ping >= 15:
                    status_box.caption(
                        f"仍在运行… 全书已 {elapsed:.0f} 秒 · "
                        f"单章 API 连续 {PIPELINE_UI_INACTIVITY_SEC // 60} 分钟无新数据将超时"
                        f"（无全书总时长限制）"
                    )
                    last_status_ping = elapsed
                time.sleep(1.5)
    finally:
        preview_live.empty()
        st.session_state.pipeline_running = False
        if _pipeline_worker_thread and not _pipeline_worker_thread.is_alive():
            _pipeline_worker_thread = None

    if outcome["error"]:
        err_msg = str(outcome["error"])
        on_log(f"[ERROR] 拆解失败: {err_msg}")
        refresh_log_panel()
        st.error(f"拆解失败：{err_msg}")
        return

    result = outcome["result"]
    st.session_state.pipeline_result = result
    progress_bar.progress(1.0)
    skip_note = (
        f"（跳过 {result.chunks_skipped} 章）" if result.chunks_skipped else ""
    )
    if getattr(result, "reprocess_only", False):
        status_box.markdown(
            f"**指定章节重跑完成 {result.chunks_total}/{result.chunks_total}**"
            f"{skip_note}。"
        )
    else:
        status_box.markdown(
            f"**步骤 {result.chunks_total}/{result.chunks_total}** · 全部完成{skip_note}。"
        )
    refresh_log_panel()

    result = st.session_state.get("pipeline_result")
    if not result:
        with get_connection() as conn:
            stats = get_pipeline_stats(conn)
            if stats["script_lines"] > 0:
                st.caption(
                    f"本地库已有数据：{stats['characters']} 个角色，"
                    f"{stats['script_lines']} 条剧本行。"
                )
        return

    book_total = int(
        getattr(result, "book_chapters_total", None) or result.chunks_total
    )
    skip_msg = (
        f"，本次跳过 **{result.chunks_skipped}** 章（节省 Token）"
        if result.chunks_skipped
        else ""
    )
    if getattr(result, "reprocess_only", False):
        script_line = (
            f"- 📜 指定章节重跑：**{result.chunks_total}** 章已完成{skip_msg} · "
            f"库内累计 **{result.script_lines}** 行 · "
            f"**{result.chapters}/{book_total}** 章有剧本\n"
        )
    else:
        script_line = (
            f"- 📜 剧本全量录入：**{result.script_lines}** 行"
            f"（库内 **{result.chapters}/{book_total}** 章有剧本{skip_msg}）\n"
        )
    st.success(
        f"**Phase 2 拆解审计报告**\n\n"
        f"{script_line}"
        f"- ⭐ 已锁定 Top **{ROLLING_RANK_TOP_N}** 核心主角（importance=main）："
        f"**{result.main_characters}** 人\n"
        f"- 🎭 龙套池（importance=extra）：**{result.extra_characters}** 人\n"
        f"- 📋 演员表合计：**{result.characters}** 人"
        + (
            f"（待分级 pending：**{result.pending_characters}**）"
            if result.pending_characters
            else ""
        )
    )


# ---------------------------------------------------------------------------
# Main area: book import center
# ---------------------------------------------------------------------------
def render_main() -> None:
    """Render title, legal gate, file uploader, and upload preview."""
    st.markdown(
        '<p class="b2a-main-title">📘 B2A-Studio | 通用多角色有声书制作工具</p>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<p class="b2a-subtitle">Book-to-Audio Studio · 本地运行</p>',
        unsafe_allow_html=True,
    )

    disclaimer_html = format_disclaimer_html(LEGAL_DISCLAIMER_TEXT)
    st.markdown(
        f"""
        <div class="b2a-legal-box">
            <h4>⚖️ 法律免责声明（使用前必读）</h4>
            {disclaimer_html}
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.checkbox(LEGAL_AGREE_CHECKBOX_LABEL, key="legal_agreed")
    legal_agreed = st.session_state.legal_agreed

    with st.container(border=True):
        st.subheader("📂 书籍导入中心")

        if not legal_agreed:
            st.warning("请先勾选上方法律免责声明，方可上传本地小说文件。")

        uploaded_file = st.file_uploader(
            "拖拽或点击上传长篇小说（仅支持 .txt）",
            type=["txt"],
            disabled=not legal_agreed,
            help="TXT · 单文件 · 最大 5 MB。勾选免责声明后解锁。",
            key="novel_file_uploader",
        )

    novel_text = st.session_state.get("uploaded_novel_text", "")
    novel_name = st.session_state.get("uploaded_novel_name", "")
    encoding_used = ""

    if uploaded_file is not None:
        if uploaded_file.size > MAX_UPLOAD_BYTES:
            st.error(
                f"文件过大（{uploaded_file.size / 1024 / 1024:.2f} MB），"
                "单文件上限为 5 MB。"
            )
            return
        try:
            novel_text, encoding_used = read_uploaded_novel(uploaded_file)
        except UnicodeDecodeError as exc:
            st.error(f"文件解码失败：{exc}")
            return
        except Exception as exc:
            st.error(f"读取文件时发生错误：{exc}")
            return
        on_novel_loaded(uploaded_file.name, novel_text)
        novel_name = uploaded_file.name

    if not novel_text:
        return

    with st.container(border=True):
        st.subheader("📖 文件概览")

        char_count = len(novel_text)
        preview = novel_text[:PREVIEW_CHAR_LIMIT]
        if char_count > PREVIEW_CHAR_LIMIT:
            preview += "\n…（以下省略）"

        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("文件名", novel_name or "—")
        with col2:
            st.metric("总字数", f"{char_count:,}")
        with col3:
            st.metric("文件编码", encoding_used or "—")

        st.markdown(f"**正文预览（前 {PREVIEW_CHAR_LIMIT} 字）**")
        st.text_area(
            "正文预览",
            preview,
            height=320,
            disabled=True,
            label_visibility="collapsed",
        )

    with st.container(border=True):
        render_pipeline_section()
        if not st.session_state.get("pipeline_running"):
            st.divider()
            try:
                render_script_debug_panel(novel_text, key_suffix="_main")
            except Exception as exc:
                st.warning(f"剧本库预览加载失败：{exc}")

    progress = get_local_book_progress(novel_text, novel_name)
    script_complete = bool(
        progress.is_complete and progress.script_lines > 0
    )

    if script_complete:
        with st.container(border=True):
            render_scroll_anchor(ANCHOR_CASTING)
            st.subheader("🎭 配音演员试镜")
            render_casting_room()

        with get_connection() as conn:
            casting_done = casting_binding_complete(conn)
        if casting_done:
            with st.expander("🔤 读音校正", expanded=False):
                render_scroll_anchor(ANCHOR_PRONUNCIATION)
                render_pronunciation_panel(
                    novel_fingerprint=st.session_state.get("novel_fingerprint", "")
                )
            with st.container(border=True):
                render_scroll_anchor(ANCHOR_RECORDING)
                st.subheader("🎙️ 有声书录音棚")
                render_audiobook_recording_studio()

    apply_pending_scroll()


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(
        page_title="B2A-Studio",
        page_icon="📘",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    init_session_state()
    ensure_database()
    inject_custom_css()
    render_sidebar()
    ensure_bundled_voices_in_session()
    render_main()


if __name__ == "__main__":
    main()
