#!/usr/bin/env python3
"""导出当前库内配音演员绑定到 B2A-Studio/backups/（JSON + CSV）。"""

from __future__ import annotations

import argparse

from .casting_backup import export_casting_backup


def main() -> None:
    parser = argparse.ArgumentParser(description="导出配音演员离线备份")
    parser.add_argument(
        "--title",
        default="",
        help="书名（用于备份文件名；默认「未命名」）",
    )
    args = parser.parse_args()
    json_path, csv_path = export_casting_backup(args.title)
    print(f"已导出 JSON: {json_path}")
    print(f"已导出 CSV:  {csv_path}")
    print(f"最新副本:    backups/casting_{args.title}_latest.json")


if __name__ == "__main__":
    main()
