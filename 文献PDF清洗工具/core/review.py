# -*- coding: utf-8 -*-
"""review.py — 标题人工校对（无编号综述必经环节）。

原则：自动识别给出初稿，人工校对定稿。
所有操作都是纯函数（输入 headings 列表，返回是否变更），方便界面调用和单测。

Heading 字段：id / level / number / text / page / order / parent_id /
             line_idx / inline / confirmed / basis
confirmed: "auto"(自动识别) / "待确认"(无编号推断) / "人工确认"(已校对)
"""
from __future__ import annotations

MAX_LEVEL = 5


# ---------------------------------------------------------------- 树重建
def rebuild_links(headings: list[dict]) -> None:
    """按顺序与层级重建 parent_id / order（层级调整、删除后调用）。"""
    stack: dict[int, str] = {}
    for n, h in enumerate(headings):
        h["order"] = n + 1
        lvl = int(h.get("level", 1) or 1)
        parent = None
        for lv in range(lvl - 1, 0, -1):
            if lv in stack:
                parent = stack[lv]
                break
        h["parent_id"] = parent
        stack[lvl] = h["id"]
        for lv in [k for k in stack if k > lvl]:
            del stack[lv]


# ---------------------------------------------------------------- 单项操作
def _find(headings: list[dict], hid: str) -> dict | None:
    for h in headings:
        if h.get("id") == hid:
            return h
    return None


def adjust_level(headings: list[dict], hid: str, delta: int) -> bool:
    """升/降级：delta=-1 升一级（数字变小），+1 降一级。"""
    h = _find(headings, hid)
    if not h:
        return False
    lvl = int(h.get("level", 1) or 1) + delta
    if lvl < 1 or lvl > MAX_LEVEL or lvl == h.get("level"):
        return False
    h["level"] = lvl
    h["confirmed"] = "人工确认"
    rebuild_links(headings)
    return True


def set_level(headings: list[dict], hid: str, level: int) -> bool:
    h = _find(headings, hid)
    if not h or not (1 <= level <= MAX_LEVEL):
        return False
    if h.get("level") == level:
        return False
    h["level"] = level
    h["confirmed"] = "人工确认"
    rebuild_links(headings)
    return True


def rename_heading(headings: list[dict], hid: str, text: str) -> bool:
    h = _find(headings, hid)
    text = (text or "").strip()
    if not h or not text or h.get("text") == text:
        return False
    h["text"] = text
    h["confirmed"] = "人工确认"
    return True


def delete_heading(headings: list[dict], hid: str) -> bool:
    """删除标题（子标题自动挂到被删标题的上级）。"""
    idx = next((i for i, h in enumerate(headings) if h.get("id") == hid), -1)
    if idx < 0:
        return False
    victim = headings[idx]
    lvl = int(victim.get("level", 1) or 1)
    # 找被删标题的父级
    parent = None
    for k in range(idx - 1, -1, -1):
        if int(headings[k].get("level", 1) or 1) < lvl:
            parent = headings[k]["id"]
            break
    for j in range(idx + 1, len(headings)):
        if headings[j].get("parent_id") == victim["id"]:
            headings[j]["parent_id"] = parent
    headings.pop(idx)
    rebuild_links(headings)
    return True


def confirm_heading(headings: list[dict], hid: str) -> bool:
    h = _find(headings, hid)
    if not h or h.get("confirmed") == "人工确认":
        return False
    h["confirmed"] = "人工确认"
    return True


def confirm_all(headings: list[dict]) -> int:
    """把全部待确认标题标记为人工确认，返回变更数量。"""
    n = 0
    for h in headings:
        if h.get("confirmed") != "人工确认":
            h["confirmed"] = "人工确认"
            n += 1
    return n


# ---------------------------------------------------------------- 快照/恢复
def snapshot(result: dict) -> None:
    """首次修改前保存自动识别的原始标题，支持一键恢复。"""
    if "_orig_headings" not in result:
        import copy
        result["_orig_headings"] = copy.deepcopy(result.get("headings", []))


def restore(result: dict) -> bool:
    """恢复为自动识别结果。"""
    orig = result.get("_orig_headings")
    if not orig:
        return False
    import copy
    result["headings"] = copy.deepcopy(orig)
    return True


# ---------------------------------------------------------------- 统计
def pending_count(result: dict) -> int:
    return sum(1 for h in result.get("headings", []) if h.get("confirmed") == "待确认")


def review_summary(result: dict) -> dict:
    heads = result.get("headings", [])
    return {
        "total": len(heads),
        "pending": pending_count(result),
        "confirmed": sum(1 for h in heads if h.get("confirmed") == "人工确认"),
        "auto": sum(1 for h in heads if h.get("confirmed") == "auto"),
    }
