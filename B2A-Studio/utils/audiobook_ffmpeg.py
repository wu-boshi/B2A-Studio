"""为 pydub 配置 FFmpeg（优先内置 imageio-ffmpeg，其次系统 PATH）。"""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path

_CONFIGURED: str | None = None


class FFmpegNotAvailable(RuntimeError):
    """本机与内置均未找到可用的 FFmpeg。"""


def _resolve_ffmpeg_path() -> str | None:
    system = shutil.which("ffmpeg")
    if system and Path(system).is_file():
        return system
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and Path(bundled).is_file():
            return bundled
    except ImportError:
        pass
    return None


def ensure_ffmpeg_configured() -> str:
    """在首次使用 pydub 前调用；配置 AudioSegment.converter。"""
    global _CONFIGURED
    if _CONFIGURED and Path(_CONFIGURED).is_file():
        return _CONFIGURED

    path = _resolve_ffmpeg_path()
    if not path:
        raise FFmpegNotAvailable(
            "未找到 FFmpeg：无法解析/导出 MP3。"
            "请在当前 Python 环境执行：\n"
            "  pip install imageio-ffmpeg\n"
            "然后 Ctrl+C 停止 Streamlit，再重新 streamlit run app.py\n"
            "或安装系统 FFmpeg：brew install ffmpeg"
        )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        from pydub import AudioSegment

        AudioSegment.converter = path
    _CONFIGURED = path
    return path


def ffmpeg_path() -> str:
    return ensure_ffmpeg_configured()


def load_mp3_segment(audio_bytes: bytes):
    """从 MP3 字节加载 AudioSegment（不依赖系统 ffprobe）。"""
    import io

    from pydub import AudioSegment

    ensure_ffmpeg_configured()
    return AudioSegment.from_file(
        io.BytesIO(audio_bytes),
        format="mp3",
        codec="mp3",
    )
