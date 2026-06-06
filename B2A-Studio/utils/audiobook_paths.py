"""有声书成品目录与章节文件命名。"""

from __future__ import annotations

import re
from pathlib import Path

from utils.b2a_paths import APP_DIR, audiobook_output_root

_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTI_SPACE = re.compile(r"\s+")


def sanitize_novel_title(name: str, *, fallback: str = "未命名小说") -> str:
    """清洗小说名中的非法路径字符，避免写入失败。"""
    raw = (name or "").strip()
    if raw.lower().endswith(".txt"):
        raw = raw[:-4].strip()
    cleaned = _INVALID_FS_CHARS.sub("_", raw)
    cleaned = _MULTI_SPACE.sub(" ", cleaned).strip(" .")
    return cleaned[:120] if cleaned else fallback


def _title_dir_variants(name: str) -> list[str]:
    """上传名可能是「沙山早月」或「《沙山早月》」，历史缓存目录两种都有。"""
    title = sanitize_novel_title(name)
    variants: list[str] = []
    for candidate in (title, title.strip("《》")):
        if candidate and candidate not in variants:
            variants.append(candidate)
    if title and not title.startswith("《"):
        wrapped = f"《{title.strip('《》')}》"
        if wrapped not in variants:
            variants.append(wrapped)
    return variants or [title or "未命名小说"]


def chapter_mp3_basename(title: str, chapter_num: int) -> str:
    """成品基名（无扩展名），如 `《沙山早月》（境风）第001章`。"""
    return f"{title}第{int(chapter_num):03d}章"


def legacy_chapter_mp3_basename(title: str, chapter_num: int) -> str:
    """旧版基名：`{title}_第{N}章`（章节号未补零）。"""
    return f"{title}_第{int(chapter_num)}章"


def _count_chapter_mp3_files(folder: Path) -> int:
    seen: set[str] = set()
    for pattern in ("*第*章.mp3", "*_第*章.mp3"):
        for path in folder.glob(pattern):
            seen.add(path.name)
    return len(seen)


def _cache_mp3_score(folder: Path) -> int:
    cache = folder / ".cache"
    if not cache.is_dir():
        return 0
    return sum(1 for _ in cache.rglob("line_*.mp3"))


_RESOLVED_OUTPUT_DIRS: dict[str, Path] = {}


def audiobook_search_roots(*, base_dir: Path | None = None) -> tuple[Path, ...]:
    """
    查找已有有声书目录时扫描的根路径。
    默认：`_local/`（写入）+ `B2A-Studio/`（历史兼容）。
    """
    if base_dir is not None:
        return (base_dir,)
    primary = audiobook_output_root()
    roots: list[Path] = [primary]
    legacy = APP_DIR.resolve()
    if legacy != primary:
        roots.append(legacy)
    seen: set[str] = set()
    out: list[Path] = []
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        out.append(root)
    return tuple(out)


def _line_cache_exists(folder: Path, chapter_num: int, line_id: int) -> bool:
    path = folder / ".cache" / f"chapter_{int(chapter_num):04d}" / f"line_{int(line_id)}.mp3"
    return path.is_file() and path.stat().st_size > 0


def cache_alignment_score(
    folder: Path,
    line_pairs: list[tuple[int, int]],
) -> int:
    """统计与当前库 script_lines.id 对齐的行级 MP3 数量。"""
    return sum(
        1 for line_id, chapter_num in line_pairs if _line_cache_exists(
            folder, chapter_num, line_id
        )
    )


def _pick_best_audiobook_folder(
    novel_display_name: str,
    roots: tuple[Path, ...],
    *,
    line_pairs: list[tuple[int, int]] | None = None,
) -> Path | None:
    best: Path | None = None
    best_aligned = -1
    best_raw = -1
    pairs = line_pairs or []
    for root in roots:
        for variant in _title_dir_variants(novel_display_name):
            candidate = root / f"{variant}_有声书"
            if not candidate.is_dir():
                continue
            aligned = cache_alignment_score(candidate, pairs) if pairs else 0
            raw = _cache_mp3_score(candidate) + _count_chapter_mp3_files(candidate) * 200
            if aligned > best_aligned or (
                aligned == best_aligned and raw > best_raw
            ):
                best_aligned = aligned
                best_raw = raw
                best = candidate
    return best


def _default_create_root(*, base_dir: Path | None = None) -> Path:
    if base_dir is not None:
        return base_dir
    return audiobook_output_root()


def refresh_audiobook_output_dir_resolution(
    novel_display_name: str,
    line_pairs: list[tuple[int, int]],
    *,
    base_dir: Path | None = None,
) -> Path:
    """
    按「行 id 与磁盘 line_{id}.mp3 是否对齐」选定有声书目录，并缓存供后续路径解析。
    新建目录时写入 `_local/`（或 B2A_AUDIOBOOK_OUTPUT_DIR）。
    """
    name_key = novel_display_name.strip()
    roots = audiobook_search_roots(base_dir=base_dir)
    best = _pick_best_audiobook_folder(
        novel_display_name, roots, line_pairs=line_pairs
    )
    if best is None:
        create_root = _default_create_root(base_dir=base_dir)
        create_root.mkdir(parents=True, exist_ok=True)
        title = sanitize_novel_title(novel_display_name)
        best = create_root / f"{title}_有声书"
        best.mkdir(parents=True, exist_ok=True)
    if base_dir is None:
        _RESOLVED_OUTPUT_DIRS[name_key] = best
    return best


def resolve_audiobook_output_dir(
    novel_display_name: str,
    *,
    base_dir: Path | None = None,
    create_if_missing: bool = True,
) -> Path:
    """
    解析实际有声书目录：优先使用 refresh 缓存；否则按目录体量启发式选择。
    新建目录时写入 `_local/`（或 B2A_AUDIOBOOK_OUTPUT_DIR）。
    """
    name_key = novel_display_name.strip()
    if base_dir is None:
        cached = _RESOLVED_OUTPUT_DIRS.get(name_key)
        if cached is not None and cached.is_dir():
            return cached

    roots = audiobook_search_roots(base_dir=base_dir)
    best = _pick_best_audiobook_folder(novel_display_name, roots)
    if best is not None:
        if base_dir is None:
            _RESOLVED_OUTPUT_DIRS[name_key] = best
        return best

    create_root = _default_create_root(base_dir=base_dir)
    title = sanitize_novel_title(novel_display_name)
    out = create_root / f"{title}_有声书"
    if create_if_missing:
        create_root.mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)
    if base_dir is None:
        _RESOLVED_OUTPUT_DIRS[name_key] = out
    return out


def folder_novel_title(folder: Path) -> str:
    """从 `xxx_有声书` 目录名还原用于成品 MP3 文件名的标题。"""
    name = folder.name
    if name.endswith("_有声书"):
        return name[: -len("_有声书")]
    return name


def audiobook_output_dir(novel_display_name: str, *, base_dir: Path | None = None) -> Path:
    """`[小说名]_有声书` 目录（自动创建；若已有历史目录则复用）。"""
    return resolve_audiobook_output_dir(
        novel_display_name, base_dir=base_dir, create_if_missing=True
    )


def chapter_mp3_path(novel_display_name: str, chapter_num: int, *, base_dir: Path | None = None) -> Path:
    """`[小说名]第NNN章.mp3`（章节号三位补零）。"""
    folder = resolve_audiobook_output_dir(
        novel_display_name, base_dir=base_dir, create_if_missing=True
    )
    title = folder_novel_title(folder)
    return folder / f"{chapter_mp3_basename(title, chapter_num)}.mp3"


def legacy_chapter_mp3_path(
    novel_display_name: str,
    chapter_num: int,
    *,
    base_dir: Path | None = None,
) -> Path:
    """旧版路径 `{title}_第{N}章.mp3`，仅用于读取/清理历史成品。"""
    folder = resolve_audiobook_output_dir(
        novel_display_name, base_dir=base_dir, create_if_missing=False
    )
    title = folder_novel_title(folder)
    return folder / f"{legacy_chapter_mp3_basename(title, chapter_num)}.mp3"


def resolve_chapter_mp3_path(
    novel_display_name: str,
    chapter_num: int,
    *,
    base_dir: Path | None = None,
) -> Path:
    """优先返回新版路径；若仅存在旧版成品则返回旧版路径。"""
    new_path = chapter_mp3_path(
        novel_display_name, chapter_num, base_dir=base_dir
    )
    if new_path.is_file():
        return new_path
    legacy = legacy_chapter_mp3_path(
        novel_display_name, chapter_num, base_dir=base_dir
    )
    if legacy.is_file():
        return legacy
    return new_path


def chapter_cache_dir(
    novel_display_name: str,
    chapter_num: int,
    *,
    base_dir: Path | None = None,
) -> Path:
    """行级缓存 WAV/MP3，用于合拢前落盘。"""
    folder = audiobook_output_dir(novel_display_name, base_dir=base_dir)
    cache = folder / ".cache" / f"chapter_{int(chapter_num):04d}"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def line_cache_audio_path(
    novel_display_name: str,
    chapter_num: int,
    line_id: int,
    *,
    base_dir: Path | None = None,
    ext: str = "mp3",
) -> Path:
    cache = chapter_cache_dir(novel_display_name, chapter_num, base_dir=base_dir)
    return cache / f"line_{int(line_id)}.{ext.lstrip('.')}"
