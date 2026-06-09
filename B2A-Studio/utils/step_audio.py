"""内置 StepAudio 2.5 音色库与 Step Plan TTS 试听。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from utils.b2a_paths import APP_DIR as _APP_DIR, B2A_ROOT
# 与官方文档「官方音色清单」一致（stepaudio-2.5-tts）
# https://platform.stepfun.com/docs/zh/guides/developer/tts
_BUNDLED_VOICES_JSON = B2A_ROOT / "data" / "step_tts2_system_voices.json"
# 清单变更时递增，促使 session 重新加载内置音色
BUNDLED_VOICES_CATALOG_REV = "stepaudio-2.5-official-36"

# 试镜大厅不展示：偏客服/助手风格，与有声书其余音色不匹配
CASTING_EXCLUDED_VOICE_IDS = frozenset(
    {
        "shuangkuainansheng",  # 爽快男声
        "ganliannvsheng",  # 干练女声
        "qinhenvsheng",  # 亲和女声
        "huolinvsheng",  # 活力女声
    }
)
CASTING_EXCLUDED_DISPLAY_NAMES = frozenset(
    {"爽快男声", "干练女声", "亲和女声", "活力女声"}
)

STEP_PLAN_API_BASE = "https://api.stepfun.com/step_plan/v1"
SPEECH_URL = f"{STEP_PLAN_API_BASE}/audio/speech"
TTS_MODEL = "stepaudio-2.5-tts"

CONNECT_TIMEOUT_SEC = 12
READ_TIMEOUT_SEC = 90


class StepAudioError(RuntimeError):
    """官方音频接口调用失败。"""


@dataclass(frozen=True)
class SystemVoice:
    """stepaudio-2.5-tts 系统音色条目。"""

    voice_id: str
    display_name: str
    description: str
    recommended_scene: str

    @property
    def select_label(self) -> str:
        parts = [self.display_name or self.voice_id]
        if self.recommended_scene:
            parts.append(self.recommended_scene)
        return " · ".join(parts)


def bundled_system_voices() -> list[SystemVoice]:
    """从内置 JSON 加载 stepaudio-2.5-tts 官方音色（与平台文档清单一致）。"""
    if _BUNDLED_VOICES_JSON.is_file():
        try:
            raw = json.loads(_BUNDLED_VOICES_JSON.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                out: list[SystemVoice] = []
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    vid = str(item.get("voice_id") or "").strip()
                    if not vid:
                        continue
                    out.append(
                        SystemVoice(
                            voice_id=vid,
                            display_name=str(
                                item.get("display_name") or vid
                            ).strip(),
                            description=str(item.get("description") or "").strip(),
                            recommended_scene=str(
                                item.get("recommended_scene") or ""
                            ).strip(),
                        )
                    )
                if out:
                    return sorted(out, key=lambda v: (v.display_name, v.voice_id))
        except (OSError, json.JSONDecodeError):
            pass
    return []


def is_casting_excluded_voice(voice: SystemVoice) -> bool:
    return (
        voice.voice_id in CASTING_EXCLUDED_VOICE_IDS
        or voice.display_name in CASTING_EXCLUDED_DISPLAY_NAMES
    )


def voices_for_casting(voices: list[SystemVoice]) -> list[SystemVoice]:
    """试镜大厅可选音色（排除风格不匹配的条目）。"""
    return [v for v in voices if not is_casting_excluded_voice(v)]


def is_casting_excluded_voice_id(
    voice_id: str,
    *,
    all_voices: list[SystemVoice] | None = None,
) -> bool:
    vid = (voice_id or "").strip()
    if not vid:
        return False
    if vid in CASTING_EXCLUDED_VOICE_IDS:
        return True
    if all_voices:
        for v in all_voices:
            if v.voice_id == vid:
                return is_casting_excluded_voice(v)
    return False


def get_system_voices() -> list[SystemVoice]:
    """返回内置音色列表；为空时抛出 StepAudioError。"""
    voices = bundled_system_voices()
    if not voices:
        raise StepAudioError(
            f"内置音色库不可用（请检查 {_BUNDLED_VOICES_JSON.name}）。"
        )
    return voices


def _auth_headers(api_key: str) -> dict[str, str]:
    key = (api_key or "").strip()
    if not key:
        raise StepAudioError("未配置 Step API Key。")
    return {"Authorization": f"Bearer {key}"}


def synthesize_casting_preview(
    api_key: str,
    *,
    voice_id: str,
    quote_text: str,
    emotion_instruction: str = "",
    model: str = TTS_MODEL,
    pronunciation_tone: list[str] | None = None,
) -> bytes:
    """
    Step Plan TTS 试听（MP3）。
    POST https://api.stepfun.com/step_plan/v1/audio/speech
    model=stepaudio-2.5-tts；语气走 instruction 字段（见官方文档）。
    """
    text = (quote_text or "").strip()
    if not text:
        raise StepAudioError("经典台词为空，无法试听。")
    voice = (voice_id or "").strip()
    if not voice:
        raise StepAudioError("请先选择音色。")

    instruction = (emotion_instruction or "").strip()

    body: dict[str, Any] = {
        "model": model,
        "voice": voice,
        "input": text[:1000],
        "response_format": "mp3",
    }
    if instruction:
        body["instruction"] = instruction[:200]
    if pronunciation_tone:
        body["pronunciation_map"] = {"tone": pronunciation_tone}

    headers = {
        **_auth_headers(api_key),
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            SPEECH_URL,
            headers=headers,
            json=body,
            timeout=(CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC),
        )
        if response.status_code >= 400:
            preview = (response.text or "")[:500]
            raise StepAudioError(
                f"TTS HTTP {response.status_code}（Step Plan）: {preview}"
            )
        content = response.content
        if not content:
            raise StepAudioError("TTS 返回空音频。")
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "json" in content_type:
            try:
                payload = response.json()
                raise StepAudioError(
                    f"TTS 返回 JSON 而非音频: {str(payload)[:300]}"
                )
            except json.JSONDecodeError:
                raise StepAudioError("TTS 返回非音频内容。") from None
        return content
    except StepAudioError:
        raise
    except requests.Timeout as exc:
        raise StepAudioError("TTS 请求超时，请稍后重试。") from exc
    except requests.RequestException as exc:
        raise StepAudioError(f"TTS 网络错误: {exc}") from exc


def voice_select_options(
    voices: list[SystemVoice],
    *,
    voice_owners: dict[str, str],
    current_character: str,
) -> tuple[list[str], dict[str, str]]:
    """
    构建 selectbox 的 value 列表与展示标签。
    voice_owners: voice_id -> 已绑定角色名（用于 [已占用] 提示，不拦截选择）。
    """
    ids = [v.voice_id for v in voices]
    labels: dict[str, str] = {}
    for v in voices:
        label = f"{v.display_name}（{v.voice_id}）"
        if v.description:
            label += f" — {v.description[:40]}"
        owner = voice_owners.get(v.voice_id, "")
        if owner and owner != current_character:
            label += f"  [{owner} 已占用]"
        labels[v.voice_id] = label
    return ids, labels


def casting_voice_select_options(
    voices: list[SystemVoice],
    *,
    voice_owners: dict[str, str],
    current_character: str,
) -> tuple[list[str], dict[str, str]]:
    """
    试镜 selectbox：候选列表已排除 CASTING_EXCLUDED_*；
    标签表仍覆盖全库，便于展示历史绑定音色名称。
    """
    candidates = voices_for_casting(voices)
    ids, labels = voice_select_options(
        candidates,
        voice_owners=voice_owners,
        current_character=current_character,
    )
    for v in voices:
        if v.voice_id in labels:
            continue
        label = f"{v.display_name}（{v.voice_id}）"
        if v.description:
            label += f" — {v.description[:40]}"
        owner = voice_owners.get(v.voice_id, "")
        if owner and owner != current_character:
            label += f"  [{owner} 已占用]"
        labels[v.voice_id] = label
    return ids, labels
