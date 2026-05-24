"""龙套音色池：试镜大厅六档龙套卡 + 按配角性别/年龄自动套用音色。"""

from __future__ import annotations

import re
from typing import Any

from db import (
    IMPORTANCE_EXTRA,
    IMPORTANCE_MAIN,
    IMPORTANCE_STOCK,
    NARRATOR_NAME,
    resolve_cast_voice_id,
    script_line_roles,
    script_role_belongs_to_cast,
)

# 试镜大厅龙套档位（固定 6 张卡）
STOCK_EXTRA_SLOTS: tuple[dict[str, str], ...] = (
    {
        "name": "男少年龙套",
        "gender": "男",
        "age_band": "少年",
        "personality": "男性少年/学生龙套，对白简短自然。",
        "quote_1": "好的，老师。",
        "quote_1_instruction": "略带稚气，语速偏快",
    },
    {
        "name": "男青年龙套",
        "gender": "男",
        "age_band": "青年",
        "personality": "男性青年龙套，日常对话、同事同学类角色。",
        "quote_1": "行，没问题。",
        "quote_1_instruction": "干脆利落",
    },
    {
        "name": "男中老年龙套",
        "gender": "男",
        "age_band": "中老年",
        "personality": "男性中年或老年龙套，长辈、上司、路人大叔等。",
        "quote_1": "嗯，我知道了。",
        "quote_1_instruction": "沉稳、略低沉",
    },
    {
        "name": "女少年龙套",
        "gender": "女",
        "age_band": "少年",
        "personality": "女性少年/学生龙套。",
        "quote_1": "嗯嗯，好呀。",
        "quote_1_instruction": "清甜、略害羞",
    },
    {
        "name": "女青年龙套",
        "gender": "女",
        "age_band": "青年",
        "personality": "女性青年龙套，店员、同事、年轻路人等。",
        "quote_1": "可以的。",
        "quote_1_instruction": "亲切自然",
    },
    {
        "name": "女中老年龙套",
        "gender": "女",
        "age_band": "中老年",
        "personality": "女性中年或老年龙套，阿姨、长辈、成熟配角等。",
        "quote_1": "哎，是这样啊。",
        "quote_1_instruction": "温和、生活化",
    },
)

STOCK_EXTRA_NAMES: frozenset[str] = frozenset(s["name"] for s in STOCK_EXTRA_SLOTS)

_SLOT_BY_BAND: dict[tuple[str, str], str] = {
    (s["gender"], s["age_band"]): s["name"] for s in STOCK_EXTRA_SLOTS
}


def is_stock_extra_name(name: str) -> bool:
    return (name or "").strip() in STOCK_EXTRA_NAMES


def normalize_gender(gender: str) -> str:
    g = (gender or "").strip()
    if not g or g in ("未知", "混合", "中性"):
        return ""
    if "女" in g:
        return "女"
    if "男" in g:
        return "男"
    return ""


def classify_age_band(age: str) -> str:
    """将演员表 age 文本归入 少年 / 青年 / 中老年。"""
    text = (age or "").strip()
    if not text or text in ("未知", "各年龄段"):
        return "青年"

    if re.search(
        r"幼|童|少年|未成年|学子|学生|小孩|儿童|十几|"
        r"1[0-7]\s*岁|10岁|11岁|12岁|13岁|14岁|15岁|16岁|17岁|18岁",
        text,
    ):
        return "少年"

    if re.search(
        r"老|中年|中老年|老年|花甲|古稀|"
        r"[4-9]\d\s*岁|四十|五十|六十|七十|八十|"
        r"三十五六|四十多|五十多|六十多|七十多",
        text,
    ):
        return "中老年"

    if re.search(
        r"年轻|青年|二十|三十|成年|2[0-9]|3[0-9]|二十多|三十多|"
        r"二十七八|三十左右",
        text,
    ):
        return "青年"

    return "青年"


def stock_slot_for_profile(gender: str, age: str) -> str:
    g = normalize_gender(gender) or "男"
    band = classify_age_band(age)
    return _SLOT_BY_BAND.get((g, band), "男青年龙套")


def ensure_stock_extra_characters(conn) -> None:
    """确保六档龙套卡存在于 actors 表（importance_level=stock）；不覆盖已有 voice_id。"""
    for slot in STOCK_EXTRA_SLOTS:
        name = slot["name"]
        existing = conn.execute(
            "SELECT * FROM characters WHERE name = ?", (name,)
        ).fetchone()
        if not existing:
            conn.execute(
                """
                INSERT INTO characters (
                    name, gender, age, personality,
                    quote_1, quote_2, quote_1_instruction, quote_2_instruction,
                    voice_id, importance_level
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    slot["gender"],
                    slot["age_band"],
                    slot["personality"],
                    slot["quote_1"],
                    "",
                    slot["quote_1_instruction"],
                    "",
                    "",
                    IMPORTANCE_STOCK,
                ),
            )
            continue

        row = dict(existing)
        if (row.get("importance_level") or "").strip() != IMPORTANCE_STOCK:
            conn.execute(
                "UPDATE characters SET importance_level = ? WHERE name = ?",
                (IMPORTANCE_STOCK, name),
            )
        if not str(row.get("personality") or "").strip():
            conn.execute(
                """
                UPDATE characters SET
                    gender = ?, age = ?, personality = ?,
                    quote_1 = ?, quote_1_instruction = ?
                WHERE name = ?
                """,
                (
                    slot["gender"],
                    slot["age_band"],
                    slot["personality"],
                    slot["quote_1"],
                    slot["quote_1_instruction"],
                    name,
                ),
            )


def sync_stock_voice_ids_from_extras(conn) -> int:
    """
    龙套卡 voice_id 为空时，从已套用音色的 extra 配角按档位反推并回填。
    （修复：配角已套用但龙套卡行未写入导致标题显示 0/6）
    """
    ensure_stock_extra_characters(conn)
    extra_rows = conn.execute(
        """
        SELECT gender, age, voice_id FROM characters
        WHERE importance_level = ?
        """,
        (IMPORTANCE_EXTRA,),
    ).fetchall()
    updated = 0
    for stock_name in STOCK_EXTRA_NAMES:
        row = conn.execute(
            "SELECT voice_id FROM characters WHERE name = ?", (stock_name,)
        ).fetchone()
        if row and str(row["voice_id"] or "").strip():
            continue
        counts: dict[str, int] = {}
        for er in extra_rows:
            if (
                stock_slot_for_profile(
                    str(er["gender"] or ""), str(er["age"] or "")
                )
                != stock_name
            ):
                continue
            vid = str(er["voice_id"] or "").strip()
            if vid:
                counts[vid] = counts.get(vid, 0) + 1
        if not counts:
            continue
        dominant = max(counts, key=counts.get)
        conn.execute(
            "UPDATE characters SET voice_id = ? WHERE name = ?",
            (dominant, stock_name),
        )
        updated += 1
    return updated


def fetch_stock_extra_characters(conn) -> list[dict[str, Any]]:
    ensure_stock_extra_characters(conn)
    sync_stock_voice_ids_from_extras(conn)
    order_names = [s["name"] for s in STOCK_EXTRA_SLOTS]
    placeholders = ",".join("?" * len(order_names))
    rows = conn.execute(
        f"""
        SELECT
            name, gender, age, personality,
            quote_1, quote_2,
            quote_1_instruction, quote_2_instruction,
            voice_id, importance_level
        FROM characters
        WHERE name IN ({placeholders})
        """,
        order_names,
    ).fetchall()
    by_name = {str(r["name"]): dict(r) for r in rows}
    out: list[dict[str, Any]] = []
    for slot in STOCK_EXTRA_SLOTS:
        name = slot["name"]
        item = by_name.get(name, {**slot, "voice_id": "", "importance_level": IMPORTANCE_STOCK})
        item["voice_id"] = resolve_cast_voice_id(
            conn, name, voice_id_from_row=str(item.get("voice_id") or "")
        )
        item["is_stock_extra"] = True
        item["is_narrator"] = False
        item["dialogue_lines"] = count_extra_cast_for_stock_slot(conn, name)
        out.append(item)
    return out


def count_extra_cast_for_stock_slot(conn, stock_name: str) -> int:
    """该龙套档将套用的 extra 配角人数（按性别+年龄档匹配）。"""
    if stock_name not in STOCK_EXTRA_NAMES:
        return 0
    ph = ",".join("?" * len(STOCK_EXTRA_NAMES))
    rows = conn.execute(
        f"""
        SELECT gender, age FROM characters
        WHERE importance_level = ? AND name NOT IN ({ph})
        """,
        (IMPORTANCE_EXTRA, *STOCK_EXTRA_NAMES),
    ).fetchall()
    return sum(
        1
        for r in rows
        if stock_slot_for_profile(str(r["gender"] or ""), str(r["age"] or ""))
        == stock_name
    )


def _load_stock_voice_map(conn) -> dict[str, str]:
    ensure_stock_extra_characters(conn)
    out: dict[str, str] = {}
    for name in STOCK_EXTRA_NAMES:
        row = conn.execute(
            "SELECT voice_id FROM characters WHERE name = ?", (name,)
        ).fetchone()
        vid = str(row["voice_id"] or "").strip() if row else ""
        if vid:
            out[name] = vid
    return out


def apply_stock_voices_to_extra_cast(conn) -> dict[str, int]:
    """
    将龙套池音色套用至 importance=extra 的配角（不改动 main / 旁白 / 龙套卡自身逻辑）。
  返回 {"characters_updated": n, "script_lines_updated": m}
    """
    stock_voices = _load_stock_voice_map(conn)
    if not stock_voices:
        return {"characters_updated": 0, "script_lines_updated": 0}

    main_names = {
        str(r[0]).strip()
        for r in conn.execute(
            "SELECT name FROM characters WHERE importance_level = ?",
            (IMPORTANCE_MAIN,),
        ).fetchall()
        if str(r[0] or "").strip()
    }
    main_names.add(NARRATOR_NAME)
    main_names |= STOCK_EXTRA_NAMES

    script_roles = script_line_roles(conn)
    all_cast_names = {
        str(r[0]).strip()
        for r in conn.execute("SELECT name FROM characters").fetchall()
        if str(r[0] or "").strip()
    }

    extra_rows = conn.execute(
        """
        SELECT name, gender, age, voice_id, importance_level
        FROM characters
        WHERE importance_level = ?
        """,
        (IMPORTANCE_EXTRA,),
    ).fetchall()

    characters_updated = 0
    lines_updated = 0

    for row in extra_rows:
        char_name = str(row["name"] or "").strip()
        if not char_name or char_name in main_names:
            continue
        slot = stock_slot_for_profile(row["gender"], row["age"])
        vid = stock_voices.get(slot, "").strip()
        if not vid:
            continue

        conn.execute(
            "UPDATE characters SET voice_id = ? WHERE name = ?",
            (vid, char_name),
        )
        characters_updated += 1

        for role in script_roles:
            if not script_role_belongs_to_cast(
                char_name, role, script_roles, all_cast_names=all_cast_names
            ):
                continue
            cur = conn.execute(
                "UPDATE script_lines SET voice_id = ? WHERE role = ?",
                (vid, role),
            )
            if cur.rowcount:
                lines_updated += int(cur.rowcount)

    return {
        "characters_updated": characters_updated,
        "script_lines_updated": lines_updated,
    }


def bind_stock_extra_voice(
    conn,
    stock_name: str,
    voice_id: str,
) -> dict[str, int]:
    """绑定某一档龙套卡，并自动套用至所有匹配的 extra 配角。"""
    name = (stock_name or "").strip()
    vid = (voice_id or "").strip()
    if name not in STOCK_EXTRA_NAMES or not vid:
        return {
            "characters_updated": 0,
            "script_lines_updated": 0,
            "stock_slot": name,
        }
    ensure_stock_extra_characters(conn)
    conn.execute(
        "UPDATE characters SET voice_id = ? WHERE name = ?",
        (vid, name),
    )
    applied = apply_stock_voices_to_extra_cast(conn)
    return {
        **applied,
        "stock_slot": name,
    }
