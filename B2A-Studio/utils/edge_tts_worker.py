"""独立 Edge TTS 子进程（不加载 Streamlit / 父进程 edge_tts 模块）。"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _edge_cli_cmd(text_file: Path, voice: str, out_path: Path) -> list[str]:
    cli = shutil.which("edge-tts")
    if not cli:
        bundled = Path(sys.executable).resolve().parent / "edge-tts"
        if bundled.is_file():
            cli = str(bundled)
    if cli:
        return [cli, "-f", str(text_file), "-v", voice, "--write-media", str(out_path)]
    return [
        sys.executable,
        "-m",
        "edge_tts",
        "-f",
        str(text_file),
        "-v",
        voice,
        "--write-media",
        str(out_path),
    ]


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "usage: edge_tts_worker.py <text_utf8_file> <voice> <output.mp3>",
            file=sys.stderr,
        )
        return 2
    text_file = Path(sys.argv[1])
    voice = sys.argv[2]
    out_path = Path(sys.argv[3])
    if not text_file.is_file():
        print(f"text file missing: {text_file}", file=sys.stderr)
        return 2
    proc = subprocess.run(
        _edge_cli_cmd(text_file, voice, out_path),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "unknown").strip()
        print(err[:2000], file=sys.stderr)
        return proc.returncode if proc.returncode else 1
    if not out_path.is_file() or out_path.stat().st_size == 0:
        print("edge output empty", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
