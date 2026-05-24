"""MP3 内嵌 SYLT 同步歌词（角色：台词，与行时间轴对齐）。"""

from __future__ import annotations

from pathlib import Path


def format_sync_lyric_line(role: str, content: str) -> str:
    name = (role or "").strip() or "旁白"
    text = (content or "").strip()
    return f"{name}：{text}"


def format_lrc_timestamp(seconds: float) -> str:
    """LRC 时间戳 [mm:ss.xx]（百分之一秒）。"""
    total_cs = max(0, int(round(float(seconds) * 100)))
    minutes, rem = divmod(total_cs, 6000)
    secs, centis = divmod(rem, 100)
    return f"[{minutes:02d}:{secs:02d}.{centis:02d}]"


def write_lrc_file(lrc_path: Path, cues: list[tuple[float, str]]) -> None:
    """写出与 MP3 同名的侧车 .lrc 文件，兼容更多播放器。"""
    lines: list[str] = []
    for sec, text in cues:
        lyric = (text or "").strip()
        if not lyric:
            continue
        lines.append(f"{format_lrc_timestamp(sec)}{lyric}")
    if not lines:
        return
    Path(lrc_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def embed_sylt_lyrics(mp3_path: Path, cues: list[tuple[float, str]]) -> None:
    """
    向 MP3 写入 ID3 SYLT 帧。
    cues: [(start_sec, lyric_text), ...]，时间与章内行起始对齐。
    """
    if not cues:
        return
    try:
        from mutagen.id3 import ID3, SYLT, Encoding
        from mutagen.mp3 import MP3
    except ImportError as exc:
        raise RuntimeError(
            "需要 mutagen 才能内嵌 SYLT，请执行 pip install mutagen"
        ) from exc

    path = Path(mp3_path)
    synced = [
        (text, max(0, int(round(sec * 1000))))
        for sec, text in cues
        if (text or "").strip()
    ]
    if not synced:
        return

    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    else:
        audio.tags.delall("SYLT")

    audio.tags.add(
        SYLT(
            encoding=Encoding.UTF8,
            lang="chi",
            format=2,
            type=1,
            text=synced,
        )
    )
    audio.save()


def apply_chapter_lyrics(
    mp3_path: Path,
    cues: list[tuple[float, str]],
) -> tuple[bool, bool]:
    """
    为章成品写入内嵌 SYLT 与同名 .lrc。
    返回 (sylt_ok, lrc_ok)。
    """
    if not cues:
        return True, True
    path = Path(mp3_path)
    lrc_path = path.with_suffix(".lrc")
    lrc_ok = True
    sylt_ok = True
    try:
        write_lrc_file(lrc_path, cues)
    except OSError:
        lrc_ok = False
    try:
        embed_sylt_lyrics(path, cues)
    except Exception:
        sylt_ok = False
    return sylt_ok, lrc_ok
