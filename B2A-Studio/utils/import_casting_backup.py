#!/usr/bin/env python3
"""从 backups/ 中的 JSON 恢复配音绑定（按角色名匹配）。"""

from __future__ import annotations

import argparse
from pathlib import Path

from .casting_backup import BACKUP_DIR, import_casting_backup, list_casting_backups


def main() -> None:
    parser = argparse.ArgumentParser(description="导入配音演员离线备份")
    parser.add_argument("path", nargs="?", help="备份 JSON 路径；省略则用 latest")
    parser.add_argument(
        "--title",
        default="",
        help="用于查找 casting_{书名}_latest.json（默认「未命名」）",
    )
    args = parser.parse_args()

    if args.path:
        backup = Path(args.path)
    else:
        candidates = list_casting_backups(args.title)
        latest = BACKUP_DIR / f"casting_{args.title}_latest.json"
        backup = latest if latest.is_file() else (candidates[0] if candidates else None)
    if backup is None or not backup.is_file():
        raise SystemExit(f"未找到备份：{args.path or args.title}")

    stats = import_casting_backup(backup)
    print(
        f"已从 {backup.name} 恢复："
        f"{stats['characters_updated']} 个角色，"
        f"{stats['script_lines_updated']} 行剧本 voice_id"
    )


if __name__ == "__main__":
    main()
