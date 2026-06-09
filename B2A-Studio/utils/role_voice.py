"""剧本 role → 演员 / 龙套音色解析（复合名、关键词回退）。"""

from __future__ import annotations

import re
from typing import Any

from db import (
    NARRATOR_NAME,
    fetch_character_profile,
    resolve_cast_voice_id,
    script_line_roles,
    script_role_belongs_to_cast,
)
from utils.extra_stock import (
    STOCK_EXTRA_NAMES,
    stock_slot_for_profile,
)

_INVALID_CAST_NAME_CHARS = frozenset("*#@\\|<>{}[]")
_MAX_CAST_NAME_LEN = 48
_ROLE_SUFFIX_SEP = re.compile(r"^[/·—\-：:\s（(]")


def is_valid_cast_name(name: str) -> bool:
    """演员表 name 允许长度与括号/斜杠复合名，仍拦截明显非法字符。"""
    n = (name or "").strip()
    if not n or n == NARRATOR_NAME:
        return False
    if len(n) > _MAX_CAST_NAME_LEN:
        return False
    return not any(ch in n for ch in _INVALID_CAST_NAME_CHARS)


def cast_name_prefix_matches_role(cast_name: str, script_role: str) -> bool:
    """
    短演员名是否为剧本 role 的前缀段（如 赵父 → 赵父（神农药钵附身））。
    """
    cast_name = (cast_name or "").strip()
    script_role = (script_role or "").strip()
    if not cast_name or not script_role or cast_name == script_role:
        return cast_name == script_role
    if not script_role.startswith(cast_name):
        return False
    if len(cast_name) < 2:
        return False
    rest = script_role[len(cast_name) :]
    if not rest:
        return True
    return bool(_ROLE_SUFFIX_SEP.match(rest))


def best_cast_name_for_script_role(
    script_role: str,
    all_cast_names: set[str],
    *,
    script_roles: set[str] | None = None,
) -> str | None:
    """为剧本 role 找最匹配的 actors.name（精确 > 最长前缀 > / 分段）。"""
    role = (script_role or "").strip()
    if not role:
        return None
    if role in all_cast_names:
        return role

    prefix_hits = sorted(
        (
            n
            for n in all_cast_names
            if cast_name_prefix_matches_role(n, role)
        ),
        key=len,
        reverse=True,
    )
    if prefix_hits:
        return prefix_hits[0]

    if "/" in role:
        for segment in (s.strip() for s in role.split("/")):
            if segment in all_cast_names:
                return segment

    if script_roles is not None:
        for cast_name in sorted(all_cast_names, key=len, reverse=True):
            if script_role_belongs_to_cast(
                cast_name,
                role,
                script_roles,
                all_cast_names=all_cast_names,
            ):
                return cast_name

    return None


def infer_stock_slot_from_role_label(role: str) -> str | None:
    """从 role 文本推断六档龙套（仅作最后兜底）。"""
    text = (role or "").strip()
    if not text or text in STOCK_EXTRA_NAMES or text == NARRATOR_NAME:
        return None

    gender = "男"
    if re.search(r"女|阿姨|大妈|小姐|姐姐|姑娘|娘|婆|奶奶|母亲|母", text):
        gender = "女"

    if re.search(r"老|父|母|叔|伯|爷|姥|婆婆|前辈|大爷|花甲", text):
        band = "中老年"
    elif re.search(r"少年|孩童|儿童|小孩|学生|幼童|童子", text):
        band = "少年"
    elif re.search(r"年轻|青年|小伙|少年", text):
        band = "青年"
    else:
        band = "青年"

    return stock_slot_for_profile(gender, band)


def _load_stock_voice_map(conn) -> dict[str, str]:
    from utils.extra_stock import fetch_stock_extra_characters

    return {
        str(item["name"]): str(item.get("voice_id") or "").strip()
        for item in fetch_stock_extra_characters(conn)
        if str(item.get("voice_id") or "").strip()
    }


def resolve_voice_for_script_role(
    conn,
    role: str,
    *,
    voice_id_from_row: str = "",
) -> tuple[str, dict[str, str], str]:
    """
    解析单行对白应使用的 voice_id。
    返回 (voice_id, profile_dict, source_tag)。
    """
    role = (role or "").strip()
    vid = (voice_id_from_row or "").strip()
    if vid:
        profile = fetch_character_profile(conn, role)
        return vid, profile, "script_line"

    profile = fetch_character_profile(conn, role)
    vid = resolve_cast_voice_id(conn, role, voice_id_from_row=profile.get("voice_id", ""))
    if vid:
        return vid, profile, "exact_cast"

    all_cast_names = {
        str(r[0]).strip()
        for r in conn.execute("SELECT name FROM characters").fetchall()
        if str(r[0] or "").strip()
    }
    script_roles = script_line_roles(conn)
    mapped = best_cast_name_for_script_role(
        role, all_cast_names, script_roles=script_roles
    )
    if mapped and mapped != role:
        profile = fetch_character_profile(conn, mapped)
        vid = resolve_cast_voice_id(
            conn, mapped, voice_id_from_row=profile.get("voice_id", "")
        )
        if vid:
            return vid, profile, f"mapped:{mapped}"

    slot = infer_stock_slot_from_role_label(role)
    if slot:
        stock_voices = _load_stock_voice_map(conn)
        vid = stock_voices.get(slot, "").strip()
        if vid:
            stock_profile = fetch_character_profile(conn, slot)
            return vid, stock_profile, f"stock:{slot}"

    return "", profile, "unresolved"


def sync_orphan_script_role_voices(conn) -> dict[str, int]:
    """
    为剧本 role 补齐 voice_id：映射已有配角或龙套关键词回退。
    同时尝试将 orphan role 写入 actors（允许长名/复合名）。
    """
    from db import upsert_character

    script_roles = script_line_roles(conn)
    all_cast_names = {
        str(r[0]).strip()
        for r in conn.execute("SELECT name FROM characters").fetchall()
        if str(r[0] or "").strip()
    }

    characters_added = 0
    for role in sorted(script_roles):
        if not role or role == NARRATOR_NAME or role in all_cast_names:
            continue
        if not is_valid_cast_name(role):
            continue
        if upsert_character(conn, {"name": role}):
            characters_added += 1
            all_cast_names.add(role)

    lines_updated = 0
    characters_updated = 0
    for role in sorted(script_roles):
        if not role or role == NARRATOR_NAME:
            continue
        vid, _, source = resolve_voice_for_script_role(conn, role)
        if not vid:
            continue
        cur = conn.execute(
            """
            UPDATE script_lines SET voice_id = ?
            WHERE role = ? AND TRIM(COALESCE(voice_id, '')) = ''
            """,
            (vid, role),
        )
        lines_updated += int(cur.rowcount or 0)
        if source.startswith("mapped:") and role in all_cast_names:
            row = conn.execute(
                "SELECT voice_id FROM characters WHERE name = ?", (role,)
            ).fetchone()
            if row and not str(row["voice_id"] or "").strip():
                conn.execute(
                    "UPDATE characters SET voice_id = ? WHERE name = ?",
                    (vid, role),
                )
                characters_updated += 1

    return {
        "characters_added": characters_added,
        "characters_updated": characters_updated,
        "script_lines_updated": lines_updated,
    }
