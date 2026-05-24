"""项目路径常量：B2A-Studio 根目录与仓库根目录。"""

from __future__ import annotations

from pathlib import Path

# B2A-Studio/（含 app.py、data/、logs/）
B2A_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = B2A_ROOT
# 仓库根（B2A-Studio 的上一级，如桌面上的「胜有声」）
REPO_ROOT = B2A_ROOT.parent
