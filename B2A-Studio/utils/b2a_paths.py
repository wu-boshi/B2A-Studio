"""项目路径常量：B2A-Studio 根目录与仓库根目录。"""

from __future__ import annotations

import os
from pathlib import Path

# B2A-Studio/（含 app.py、data/、logs/）
B2A_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = B2A_ROOT
# 仓库根（B2A-Studio 的上一级，如桌面上的「胜有声」）
REPO_ROOT = B2A_ROOT.parent
# 本地数据区（.gitignore，小说/成品不入库）
LOCAL_DATA_DIR = REPO_ROOT / "_local"


def audiobook_output_root() -> Path:
    """
    有声书成品与行级缓存的默认写入根目录。
    可通过环境变量 B2A_AUDIOBOOK_OUTPUT_DIR 覆盖。
    """
    override = (os.environ.get("B2A_AUDIOBOOK_OUTPUT_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return LOCAL_DATA_DIR.resolve()

