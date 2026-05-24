"""有声书录制日志：终端风格单行，HH:MM:SS | 动作 | 结果 | 详情。"""

from __future__ import annotations

import re
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable

from utils.b2a_paths import APP_DIR, B2A_ROOT

LOG_DIR = APP_DIR / "logs"
LOG_FILE = LOG_DIR / "recording.log"

_lock = threading.Lock()
_RING: deque[str] = deque(maxlen=600)
_WS = re.compile(r"\s+")


def _compact(text: str) -> str:
    return _WS.sub(" ", (text or "").strip())


def _short(text: str, limit: int = 160) -> str:
    t = _compact(text)
    if len(t) <= limit:
        return t
    return t[: limit - 3] + "..."


def format_rec_line(
    action: str,
    result: str,
    detail: str = "",
    *,
    at: datetime | None = None,
) -> str:
    """`14:32:05 | 动作 | 结果 | 详情`"""
    stamp = (at or datetime.now()).strftime("%H:%M:%S")
    parts = [_compact(action), _compact(result)]
    if detail:
        parts.append(_compact(detail))
    body = " | ".join(p for p in parts if p)
    return f"{stamp} | {body}"


def format_rec_error(
    action: str,
    error: str,
    measure: str,
    outcome: str,
    next_step: str,
    *,
    at: datetime | None = None,
) -> str:
    detail = (
        f"原因={_short(error)}"
        f" | 措施={_compact(measure)}"
        f" | 结果={_compact(outcome)}"
        f" | 下一步={_compact(next_step)}"
    )
    return format_rec_line(action, "FAIL", detail, at=at)


def _write_line(line: str) -> None:
    with _lock:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        _RING.append(line)


class RecordingDebugLog:
    """结构化录制日志；同时写文件、内存环与可选 UI 回调。"""

    def __init__(self, *, on_line: Callable[[str], None] | None = None) -> None:
        self._on_line = on_line

    def line(
        self,
        action: str,
        result: str,
        detail: str = "",
    ) -> str:
        entry = format_rec_line(action, result, detail)
        _write_line(entry)
        if self._on_line:
            self._on_line(entry)
        return entry

    def ok(self, action: str, result: str = "", **fields: str) -> str:
        extra = " ".join(f"{k}={v}" for k, v in fields.items() if v)
        return self.line(action, f"OK {result}".strip(), extra)

    def warn(self, action: str, result: str, detail: str = "") -> str:
        return self.line(action, f"WARN {result}".strip(), detail)

    def fail(
        self,
        action: str,
        error: str,
        measure: str,
        outcome: str,
        next_step: str,
    ) -> str:
        entry = format_rec_error(action, error, measure, outcome, next_step)
        _write_line(entry)
        if self._on_line:
            self._on_line(entry)
        return entry

    def skip(self, action: str, reason: str) -> str:
        return self.line(action, "SKIP", reason)


def snapshot_recording_log(*, max_lines: int = 200) -> list[str]:
    with _lock:
        if _RING:
            return list(_RING)[-max_lines:]
    return read_recording_log_tail(max_lines=max_lines)


def read_recording_log_tail(*, max_lines: int = 200, level: str = "") -> list[str]:
    """读取文件尾部；level 保留兼容旧文件筛选，新格式可传空读全部。"""
    if not LOG_FILE.is_file():
        return []
    want = (level or "").strip().upper()
    with _lock:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    if want:
        lines = [ln for ln in lines if f"[{want}]" in ln]
    else:
        # 优先新格式（带 HH:MM:SS |）
        compact = [ln for ln in lines if re.match(r"\d{2}:\d{2}:\d{2} \|", ln)]
        if compact:
            lines = compact
    return lines[-max_lines:]


# —— 兼容旧调用 ——
def append_recording_info(action: str, result: str = "", detail: str = "") -> None:
    _write_line(format_rec_line(action, result, detail))


def append_recording_debug(action: str, result: str = "", detail: str = "") -> None:
    _write_line(format_rec_line(action, result, detail))


def append_recording_log(line: str) -> None:
    """自由文本 → 尽力解析为单行。"""
    text = _compact(line)
    if not text:
        return
    if re.match(r"\d{2}:\d{2}:\d{2} \|", text):
        _write_line(text)
        return
    _write_line(format_rec_line("日志", text))
