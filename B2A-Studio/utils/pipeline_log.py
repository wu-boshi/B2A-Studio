"""In-memory + file pipeline logger with UI callback support."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

LogLineCallback = Callable[[str], None]

from utils.b2a_paths import APP_DIR, B2A_ROOT
LOG_DIR = APP_DIR / "logs"
LOG_FILE = LOG_DIR / "pipeline.log"

_file_logger = logging.getLogger("b2a.pipeline.file")


def setup_file_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if _file_logger.handlers:
        return
    _file_logger.setLevel(logging.INFO)
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    _file_logger.addHandler(handler)


@dataclass
class PipelineLog:
    """Collect log lines for on-page display and optional file mirror."""

    on_line: LogLineCallback | None = None
    lines: list[str] = field(default_factory=list)

    def emit(self, level: str, message: str, *, to_ui: bool = True) -> None:
        setup_file_logging()
        _file_logger.info(message)
        if not to_ui:
            return
        line = f"{time.strftime('%H:%M:%S')} [{level}] {message}"
        self.lines.append(line)
        if self.on_line:
            self.on_line(line)

    def progress(self, message: str) -> None:
        """用户可见的关键进展（页面日志 + 文件）。"""
        self.emit("INFO", message, to_ui=True)

    def detail(self, message: str) -> None:
        """仅写入 logs/pipeline.log，不刷屏页面。"""
        self.emit("INFO", message, to_ui=False)

    def info(self, message: str) -> None:
        self.detail(message)

    def warn(self, message: str, *, to_ui: bool = True) -> None:
        self.emit("WARN", message, to_ui=to_ui)

    def error(self, message: str) -> None:
        self.emit("ERROR", message, to_ui=True)

    def text(self) -> str:
        return "\n".join(self.lines)
