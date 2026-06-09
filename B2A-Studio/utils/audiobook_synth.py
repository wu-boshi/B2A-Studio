"""TTS 合成：Step 主引擎 → 标点切片 → 仅审核失败切片走 Edge → 额度/超时挂起。"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from .audiobook_assembly import (
    SILENCE_PLACEHOLDER_SEC,
    audio_duration_seconds,
    make_silence_mp3_bytes,
)
from .extra_stock import classify_age_band, normalize_gender
from .recording_log import RecordingDebugLog, format_rec_line
from .step_audio import SPEECH_URL, TTS_MODEL, StepAudioError

LogFn = Callable[[str], None]

CHUNK_SPLIT_RE = re.compile(r"(?<=[，,。！？!?；;：:、])")
_HAS_SPEECH_CHAR_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
    r"a-zA-Z0-9"
    r"]"
)
MAX_INPUT_CHARS = 1000
MAX_INSTRUCTION_CHARS = 200
QUOTA_POLL_INTERVAL_SEC = 600
CONNECT_TIMEOUT_SEC = 12
READ_TIMEOUT_SEC = 120
EDGE_MAX_ATTEMPTS = 5
EDGE_RETRY_BASE_SEC = 4
STEP_CHUNK_MAX_ATTEMPTS = 3
STEP_RETRY_BACKOFF_SEC = 2.5
STEP_DURATION_ANOMALY_WAIT_SEC = int(
    os.environ.get("B2A_STEP_DURATION_RETRY_WAIT_SEC", "300")
)
STEP_DURATION_ANOMALY_RETRIES = 2
DURATION_SEC_PER_CHAR = 0.38
DURATION_MIN_SEC = 0.3
DURATION_PADDING_SEC = 2.0
EDGE_TTS_MIN_VERSION = (7, 2, 7)
EDGE_IMPL = "worker-v3"
_EDGE_WORKER = Path(__file__).resolve().with_name("edge_tts_worker.py")

EDGE_VOICE_MAP: dict[tuple[str, str], str] = {
    ("男", "少年"): "zh-CN-YunjianNeural",
    ("女", "少年"): "zh-CN-XiaoniNeural",
    ("男", "青年"): "zh-CN-YunxiNeural",
    ("女", "青年"): "zh-CN-XiaoxiaoNeural",
    ("男", "中年"): "zh-CN-YunyangNeural",
    ("女", "中年"): "zh-CN-XiaoyuNeural",
    ("男", "老年"): "zh-CN-YunjieNeural",
    ("女", "老年"): "zh-CN-XiaoruiNeural",
}


@dataclass
class SynthResult:
    audio_bytes: bytes
    actual_voice_id: str
    engine: str  # step | edge | silence
    used_chunking: bool = False


class QuotaWaitRequired(RuntimeError):
    """需进入额度挂起轮询，由上层捕获后休眠重试。"""


class RecordingPaused(Exception):
    """用户暂停录制。"""


class LineRecordingFailed(Exception):
    """单行 TTS 全部防线失败；上层记库后跳过该行、续录时重试。"""


class _CensorshipBlocked(StepAudioError):
    """Step 内容审核拦截，可对当前切片尝试 Edge。"""


def _emit(
    dbg: RecordingDebugLog | LogFn | None,
    action: str,
    result: str,
    detail: str = "",
) -> None:
    if isinstance(dbg, RecordingDebugLog):
        dbg.line(action, result, detail)
    elif dbg:
        dbg(format_rec_line(action, result, detail))


def _emit_fail(
    dbg: RecordingDebugLog | LogFn | None,
    action: str,
    error: str,
    measure: str,
    outcome: str,
    next_step: str,
) -> None:
    if isinstance(dbg, RecordingDebugLog):
        dbg.fail(action, error, measure, outcome, next_step)
    elif dbg:
        from utils.recording_log import format_rec_error

        dbg(format_rec_error(action, error, measure, outcome, next_step))


def check_edge_tts_version() -> tuple[bool, str]:
    try:
        from importlib.metadata import version

        raw = version("edge-tts")
    except Exception:
        return False, "未安装 edge-tts"
    parts: list[int] = []
    for piece in raw.split(".")[:3]:
        try:
            parts.append(int(piece))
        except ValueError:
            break
    while len(parts) < 3:
        parts.append(0)
    ok = tuple(parts) >= EDGE_TTS_MIN_VERSION
    return ok, raw


def is_punctuation_only_content(text: str) -> bool:
    return _HAS_SPEECH_CHAR_RE.search((text or "").strip()) is None


def count_speech_chars(text: str) -> int:
    spoken = (text or "").strip()
    if not spoken:
        return 0
    n = len(_HAS_SPEECH_CHAR_RE.findall(spoken))
    return n if n > 0 else len(spoken)


def expected_max_duration_seconds(text: str) -> float:
    """按字数估算 Step 整句音频合理上限（秒）。"""
    n = count_speech_chars(text)
    if n <= 0:
        return float(SILENCE_PLACEHOLDER_SEC) + 0.5
    base = max(
        DURATION_MIN_SEC,
        n * DURATION_SEC_PER_CHAR + DURATION_PADDING_SEC,
    )
    if n <= 8:
        return min(base, max(5.5, n * 0.95 + 1.8))
    if n <= 40:
        return min(base, n * 0.55 + 6.0)
    return min(base, 150.0)


def is_abnormal_step_duration(text: str, duration_sec: float) -> bool:
    if duration_sec <= 0:
        return True
    expected = expected_max_duration_seconds(text)
    if duration_sec <= expected:
        return False
    n = count_speech_chars(text)
    if n <= 12 and duration_sec > max(expected * 2.2, expected + 6.0):
        return True
    return duration_sec > max(expected * 1.6, expected + 12.0)


def _sleep_interruptible(
    seconds: float,
    pause_check: Callable[[], bool] | None,
) -> None:
    remaining = int(max(0, seconds))
    while remaining > 0:
        if pause_check and pause_check():
            raise RecordingPaused("录制已暂停")
        step = min(30, remaining)
        time.sleep(step)
        remaining -= step


def _is_transient_http(status: int) -> bool:
    return status in (408, 429, 500, 502, 503, 504)


def _auth_headers(api_key: str) -> dict[str, str]:
    key = (api_key or "").strip()
    if not key:
        raise StepAudioError("未配置 Step API Key。")
    header_val = f"Bearer {key}"
    try:
        header_val.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise StepAudioError(
            "API Key 含非 ASCII 字符，无法发送请求；请检查侧边栏密钥是否粘贴正确。"
        ) from exc
    return {"Authorization": header_val}


def _is_quota_error(status: int, body: str) -> bool:
    b = (body or "").lower()
    if status == 402:
        return True
    if status == 429 and "quota" in b:
        return True
    return "quota" in b and "exceed" in b


def _is_censorship_error(status: int, body: str) -> bool:
    b = (body or "").lower()
    if status == 451:
        return True
    return "censorship" in b or "blocked" in b and "content" in b


def edge_voice_for_profile(gender: str, age: str) -> str:
    g = normalize_gender(gender) or "男"
    text = (age or "").strip()
    if re.search(r"老|花甲|古稀|耄", text):
        band = "老年"
    elif re.search(r"中年", text):
        band = "中年"
    else:
        band = classify_age_band(age)
        if band == "中老年":
            band = "老年"
    return EDGE_VOICE_MAP.get((g, band), EDGE_VOICE_MAP[("男", "青年")])


def split_text_chunks(
    text: str,
    *,
    min_chunk: int = 4,
    aggressive: bool = False,
) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in CHUNK_SPLIT_RE.split(text) if p.strip()]
    if not parts:
        return [text[:MAX_INPUT_CHARS]]
    if aggressive:
        return [p[:MAX_INPUT_CHARS] for p in parts if p]
    merged: list[str] = []
    buf = ""
    for part in parts:
        if not buf:
            buf = part
        elif len(buf) + len(part) <= 80:
            buf += part
        else:
            if len(buf) >= min_chunk:
                merged.append(buf)
            buf = part
    if buf:
        merged.append(buf)
    return merged or [text[:MAX_INPUT_CHARS]]


def split_text_for_censorship_retry(text: str) -> list[str]:
    chunks = split_text_chunks(text, aggressive=True)
    if len(chunks) > 1:
        return chunks
    text = (text or "").strip()
    if len(text) <= 36:
        return [text]
    return [text[i : i + 36] for i in range(0, len(text), 36)]


def _step_tts_once(
    api_key: str,
    *,
    voice_id: str,
    text: str,
    instruction: str,
    pronunciation_tone: list[str] | None = None,
) -> tuple[bytes, int, str]:
    spoken = (text or "").strip() or "。"
    body: dict = {
        "model": TTS_MODEL,
        "voice": (voice_id or "").strip(),
        "input": spoken[:MAX_INPUT_CHARS],
        "response_format": "mp3",
    }
    inst = (instruction or "").strip()
    if inst:
        body["instruction"] = inst[:MAX_INSTRUCTION_CHARS]
    if pronunciation_tone:
        body["pronunciation_map"] = {"tone": pronunciation_tone}

    response = requests.post(
        SPEECH_URL,
        headers={**_auth_headers(api_key), "Content-Type": "application/json"},
        json=body,
        timeout=(CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC),
    )
    preview = (response.text or "")[:500]
    if response.status_code >= 400:
        return b"", response.status_code, preview
    content = response.content or b""
    if not content:
        return b"", response.status_code, preview or "empty audio"
    if "json" in (response.headers.get("Content-Type") or "").lower():
        return b"", response.status_code, preview
    return content, response.status_code, preview


def _edge_tts_subprocess(spoken: str, voice: str, *, timeout_sec: int = 90) -> bytes:
    """经 edge_tts_worker 独立进程调用 edge-tts CLI，父进程不 import edge_tts。"""
    if not _EDGE_WORKER.is_file():
        raise StepAudioError(f"缺少 Edge worker: {_EDGE_WORKER}")

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".txt", delete=False
    ) as tf:
        tf.write(spoken)
        text_path = Path(tf.name)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        out_path = Path(tmp.name)
    try:
        proc = subprocess.run(
            [sys.executable, str(_EDGE_WORKER), str(text_path), voice, str(out_path)],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            start_new_session=True,
            cwd=str(_EDGE_WORKER.parent),
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "unknown").strip()[:500]
            raise StepAudioError(f"Edge worker exit {proc.returncode}: {err}")
        data = out_path.read_bytes()
        if not data:
            raise StepAudioError("Edge worker 写出空 mp3")
        return data
    finally:
        for p in (text_path, out_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def _edge_tts_once(
    text: str,
    *,
    gender: str,
    age: str,
    instruction: str = "",
    dbg: RecordingDebugLog | LogFn | None = None,
) -> bytes:
    """Edge 仅走独立 worker 子进程，不在录制线程内使用 WebSocket。"""
    voice = edge_voice_for_profile(gender, age)
    # Edge 不支持语气指令参数，仅朗读正文
    spoken = ((text or "").strip() or "。")[:MAX_INPUT_CHARS]

    _emit(dbg, "Edge", "开始", f"worker {EDGE_IMPL} voice={voice}")
    try:
        audio = _edge_tts_subprocess(spoken, voice)
        _emit(dbg, "Edge", "OK", f"worker {len(audio)}B")
        return audio
    except Exception as exc:
        raise StepAudioError(
            f"Edge worker 失败 [{EDGE_IMPL}]: {_short_edge_err(exc, 280)}"
        ) from exc


def _short_edge_err(exc: BaseException, limit: int = 120) -> str:
    msg = str(exc).replace("\n", " ")
    return msg if len(msg) <= limit else msg[: limit - 3] + "..."


def _edge_tts_with_retry(
    text: str,
    *,
    gender: str,
    age: str,
    instruction: str = "",
    dbg: RecordingDebugLog | LogFn | None = None,
    pause_check: Callable[[], bool] | None = None,
    chunk_tag: str = "",
) -> bytes:
    edge_ok, edge_ver = check_edge_tts_version()
    if not edge_ok:
        _emit(
            dbg,
            "Edge版本",
            "WARN",
            f"edge-tts {edge_ver} 过旧 需>=7.2.7",
        )
    last_err: Exception | None = None
    for attempt in range(1, EDGE_MAX_ATTEMPTS + 1):
        if pause_check and pause_check():
            raise RecordingPaused("录制已暂停")
        try:
            audio = _edge_tts_once(
                text,
                gender=gender,
                age=age,
                instruction=instruction,
                dbg=dbg,
            )
            _emit(
                dbg,
                f"Edge切片{chunk_tag}",
                "OK",
                f"第{attempt}次 voice={edge_voice_for_profile(gender, age)}",
            )
            return audio
        except RecordingPaused:
            raise
        except Exception as exc:
            last_err = exc
            if attempt < EDGE_MAX_ATTEMPTS:
                wait = EDGE_RETRY_BASE_SEC * attempt
                _emit(
                    dbg,
                    f"Edge切片{chunk_tag}",
                    "WARN",
                    f"第{attempt}次失败 {exc} {wait}s后重试",
                )
                time.sleep(wait)
            else:
                _emit_fail(
                    dbg,
                    f"Edge切片{chunk_tag}",
                    str(exc),
                    f"已重试{EDGE_MAX_ATTEMPTS}次",
                    "仍失败",
                    "该行记为失败 断点续录可重试",
                )
    raise LineRecordingFailed(
        f"Edge-TTS 连续 {EDGE_MAX_ATTEMPTS} 次失败: {last_err}"
    ) from last_err


def _concat_mp3_chunks(chunks: list[bytes]) -> bytes:
    from pydub import AudioSegment

    from utils.audiobook_ffmpeg import ensure_ffmpeg_configured, load_mp3_segment

    if not chunks:
        return b""
    if len(chunks) == 1:
        return chunks[0]
    ensure_ffmpeg_configured()
    combined = AudioSegment.empty()
    for raw in chunks:
        combined += load_mp3_segment(raw)
    out = io.BytesIO()
    combined.export(out, format="mp3")
    return out.getvalue()


def _wait_step_unavailable(
    api_key: str,
    *,
    reason: str,
    dbg: RecordingDebugLog | LogFn | None,
    pause_check: Callable[[], bool] | None,
    heartbeat_voice: str = "ruyananshi",
) -> None:
    _emit_fail(
        dbg,
        "Step服务",
        reason,
        f"挂起等待 每{QUOTA_POLL_INTERVAL_SEC}s心跳",
        "不降级Edge",
        "恢复后自动续录当前切片",
    )
    while True:
        if pause_check and pause_check():
            raise RecordingPaused("录制已暂停")
        for remaining in range(QUOTA_POLL_INTERVAL_SEC, 0, -30):
            if pause_check and pause_check():
                raise RecordingPaused("录制已暂停")
            if remaining in (QUOTA_POLL_INTERVAL_SEC, 300, 60, 30):
                _emit(dbg, "Step挂起", "等待中", f"约{remaining}s后心跳")
            time.sleep(min(30, remaining))
        try:
            audio, status, body = _step_tts_once(
                api_key,
                voice_id=heartbeat_voice,
                text="测试",
                instruction="",
            )
            if status < 400 and audio:
                _emit(dbg, "Step心跳", "OK", "服务恢复 继续合成")
                return
            if _is_quota_error(status, body):
                _emit(dbg, "Step心跳", "WARN", f"仍额度受限 HTTP{status}")
                continue
            if status < 400 or _is_transient_http(status):
                _emit(dbg, "Step心跳", "OK", f"HTTP{status} 继续主流程")
                return
            _emit(dbg, "Step心跳", "WARN", f"HTTP{status} {body[:60]}")
        except requests.RequestException as exc:
            _emit(dbg, "Step心跳", "WARN", f"请求失败 {exc}")


def _step_audio_with_duration_guard(
    api_key: str,
    *,
    voice_id: str,
    text: str,
    instruction: str,
    gender: str,
    age: str,
    dbg: RecordingDebugLog | LogFn | None,
    pause_check: Callable[[], bool] | None,
    tag: str = "",
    pronunciation_tone: list[str] | None = None,
) -> SynthResult:
    """
    Step 合成；若时长明显异常则等待 5 分钟后重试 Step（共 2 次），
    仍异常则 Edge 兜底（非审核问题，多为模型故障）。
    """
    label = f"Step{tag}" if tag else "Step"
    for attempt in range(STEP_DURATION_ANOMALY_RETRIES + 1):
        if pause_check and pause_check():
            raise RecordingPaused("录制已暂停")
        audio = _step_piece(
            api_key,
            voice_id=voice_id,
            piece=text,
            instruction=instruction,
            dbg=dbg,
            pause_check=pause_check,
            tag=tag,
            pronunciation_tone=pronunciation_tone,
        )
        dur = audio_duration_seconds(audio)
        if not is_abnormal_step_duration(text, dur):
            return SynthResult(
                audio_bytes=audio,
                actual_voice_id=voice_id,
                engine="step",
            )
        expected = expected_max_duration_seconds(text)
        if attempt < STEP_DURATION_ANOMALY_RETRIES:
            wait_min = STEP_DURATION_ANOMALY_WAIT_SEC // 60
            _emit_fail(
                dbg,
                f"{label}时长",
                f"{dur:.1f}s 超出预期≤{expected:.1f}s",
                f"等待{wait_min}分钟后重试Step",
                f"第{attempt + 1}/{STEP_DURATION_ANOMALY_RETRIES}次",
                "疑为模型故障 非审核拦截",
            )
            _sleep_interruptible(STEP_DURATION_ANOMALY_WAIT_SEC, pause_check)
        else:
            _emit_fail(
                dbg,
                f"{label}时长",
                f"{dur:.1f}s 仍超≤{expected:.1f}s",
                "Step重试已用尽 改Edge兜底",
                "尝试中",
                "Edge失败则本行入库失败",
            )

    edge_voice = edge_voice_for_profile(gender, age)
    audio = _edge_tts_with_retry(
        text,
        gender=gender,
        age=age,
        instruction=instruction,
        dbg=dbg,
        pause_check=pause_check,
        chunk_tag=tag,
    )
    return SynthResult(
        audio_bytes=audio,
        actual_voice_id=f"edge:{edge_voice}",
        engine="edge",
    )


def _step_piece(
    api_key: str,
    *,
    voice_id: str,
    piece: str,
    instruction: str,
    dbg: RecordingDebugLog | LogFn | None,
    pause_check: Callable[[], bool] | None,
    tag: str = "",
    pronunciation_tone: list[str] | None = None,
) -> bytes:
    label = f"Step{tag}" if tag else "Step"
    while True:
        if pause_check and pause_check():
            raise RecordingPaused("录制已暂停")
        for attempt in range(1, STEP_CHUNK_MAX_ATTEMPTS + 1):
            if pause_check and pause_check():
                raise RecordingPaused("录制已暂停")
            try:
                audio, status, body = _step_tts_once(
                    api_key,
                    voice_id=voice_id,
                    text=piece,
                    instruction=instruction,
                    pronunciation_tone=pronunciation_tone,
                )
            except requests.RequestException as exc:
                is_timeout = isinstance(
                    exc, (requests.Timeout, requests.ReadTimeout)
                ) or "timed out" in str(exc).lower()
                kind = "超时" if is_timeout else "网络"
                if attempt < STEP_CHUNK_MAX_ATTEMPTS:
                    wait = STEP_RETRY_BACKOFF_SEC * attempt
                    _emit(
                        dbg,
                        label,
                        "WARN",
                        f"{kind} 第{attempt}/{STEP_CHUNK_MAX_ATTEMPTS}次 {wait}s后重试",
                    )
                    time.sleep(wait)
                    continue
                _wait_step_unavailable(
                    api_key,
                    reason=f"{kind} 已重试{STEP_CHUNK_MAX_ATTEMPTS}次仍失败",
                    dbg=dbg,
                    pause_check=pause_check,
                )
                continue

            if status < 400 and audio:
                _emit(
                    dbg,
                    label,
                    "OK",
                    f"voice={voice_id} {len(piece)}字",
                )
                return audio
            if _is_quota_error(status, body):
                _wait_step_unavailable(
                    api_key,
                    reason=f"额度 HTTP{status}",
                    dbg=dbg,
                    pause_check=pause_check,
                )
                continue
            if _is_censorship_error(status, body):
                raise _CensorshipBlocked(f"HTTP{status} {body[:120]}")
            if _is_transient_http(status) and attempt < STEP_CHUNK_MAX_ATTEMPTS:
                wait = STEP_RETRY_BACKOFF_SEC * attempt
                _emit(
                    dbg,
                    label,
                    "WARN",
                    f"HTTP{status} 第{attempt}/{STEP_CHUNK_MAX_ATTEMPTS}次 {wait}s后重试",
                )
                time.sleep(STEP_RETRY_BACKOFF_SEC * attempt)
                continue
            if _is_transient_http(status):
                raise StepAudioError(f"HTTP{status} {body[:120]}")
            raise StepAudioError(f"HTTP{status} {body[:120]}")
        _wait_step_unavailable(
            api_key,
            reason="切片重试次数用尽",
            dbg=dbg,
            pause_check=pause_check,
        )


def _synthesize_chunked_line(
    api_key: str,
    *,
    text: str,
    voice_id: str,
    instruction: str,
    gender: str,
    age: str,
    chunks: list[str],
    dbg: RecordingDebugLog | LogFn | None,
    pause_check: Callable[[], bool] | None,
    pronunciation_tone: list[str] | None = None,
) -> SynthResult:
    parts: list[bytes] = []
    used_edge = False
    _emit(dbg, "切片合成", "开始", f"共{len(chunks)}段")
    for i, piece in enumerate(chunks, 1):
        if pause_check and pause_check():
            raise RecordingPaused("录制已暂停")
        tag = f"{i}/{len(chunks)}"
        try:
            part_result = _step_audio_with_duration_guard(
                api_key,
                voice_id=voice_id,
                text=piece,
                instruction=instruction,
                gender=gender,
                age=age,
                dbg=dbg,
                pause_check=pause_check,
                tag=tag,
                pronunciation_tone=pronunciation_tone,
            )
            parts.append(part_result.audio_bytes)
            if part_result.engine == "edge":
                used_edge = True
        except RecordingPaused:
            raise
        except _CensorshipBlocked as exc:
            _emit_fail(
                dbg,
                f"切片{tag}",
                str(exc),
                "Step审核拦截 改Edge兜底",
                "尝试中",
                "Edge失败则该行入库失败",
            )
            parts.append(
                _edge_tts_with_retry(
                    piece,
                    gender=gender,
                    age=age,
                    instruction=instruction,
                    dbg=dbg,
                    pause_check=pause_check,
                    chunk_tag=tag,
                )
            )
            used_edge = True
        except StepAudioError as exc:
            _emit_fail(
                dbg,
                f"切片{tag}",
                str(exc),
                "仅重试Step 不降级Edge",
                "失败",
                "跳过本行 续录重试",
            )
            raise LineRecordingFailed(
                f"子句 Step 失败: {piece[:40]} · {exc}"
            ) from exc

    merged = _concat_mp3_chunks(parts)
    engine = "edge" if used_edge else "step"
    voice_tag = (
        voice_id if engine == "step" else f"edge:{edge_voice_for_profile(gender, age)}"
    )
    _emit(dbg, "切片合成", "OK", f"engine={engine} {len(chunks)}段")
    return SynthResult(
        audio_bytes=merged,
        actual_voice_id=voice_tag,
        engine=engine,
        used_chunking=True,
    )


def synthesize_line_audio_with_retry(
    api_key: str,
    *,
    content: str,
    voice_id: str,
    instruction: str = "",
    age: str = "",
    gender: str = "",
    log: LogFn | None = None,
    log_debug: RecordingDebugLog | LogFn | None = None,
    pause_check: Callable[[], bool] | None = None,
    pronunciation_tone: list[str] | None = None,
) -> SynthResult:
    dbg = log_debug or log
    text = (content or "").strip()

    if is_punctuation_only_content(text):
        sec = SILENCE_PLACEHOLDER_SEC
        _emit(dbg, "纯标点行", "OK", f"静音占位{sec}s 不调Step/Edge")
        return SynthResult(
            audio_bytes=make_silence_mp3_bytes(sec),
            actual_voice_id=f"silence:{sec}s",
            engine="silence",
        )

    _emit(dbg, "行合成", "开始", f"{len(text)}字 voice={voice_id}")

    try:
        result = _step_audio_with_duration_guard(
            api_key,
            voice_id=voice_id,
            text=text,
            instruction=instruction,
            gender=gender,
            age=age,
            dbg=dbg,
            pause_check=pause_check,
            tag="整句",
            pronunciation_tone=pronunciation_tone,
        )
        _emit(
            dbg,
            "行合成",
            "OK",
            f"{result.engine}整句 {len(text)}字",
        )
        return result
    except RecordingPaused:
        raise
    except _CensorshipBlocked:
        _emit(dbg, "行合成", "WARN", "整句审核拦截 改切片重试")
    except StepAudioError as exc:
        _emit(dbg, "行合成", "WARN", f"整句失败 {exc} 改切片")

    chunks = split_text_for_censorship_retry(text)
    if len(chunks) <= 1:
        chunks = split_text_chunks(text) or [text]

    result = _synthesize_chunked_line(
        api_key,
        text=text,
        voice_id=voice_id,
        instruction=instruction,
        gender=gender,
        age=age,
        chunks=chunks,
        dbg=dbg,
        pause_check=pause_check,
        pronunciation_tone=pronunciation_tone,
    )
    _emit(
        dbg,
        "行合成",
        "OK",
        f"{result.engine}{'+切片' if result.used_chunking else ''} {len(text)}字",
    )
    return result
