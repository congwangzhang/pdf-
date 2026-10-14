# -*- coding: utf-8 -*-
"""clean.py — 文本清洗规则（分条可开关，全部记录修改日志）。

设计原则（来自需求文档）：
  * 原始层(raw)与清洗层(clean)分开保存，清洗不覆盖原文。
  * 先标记后删除：噪声行剔除进 clean_text，但仍留在 raw_text。
  * 每条规则的修改次数都进 changelog，可追溯、可回退（重新生成即可）。
  * 不过度清洗：引用标记、科学符号、真正的连字符一律保留。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .extract import Line


# ---------------------------------------------------------------- 规则开关
@dataclass
class CleanConfig:
    fix_ligatures: bool = True        # 连字修复 ﬁ → fi
    remove_control: bool = True       # 软连字符/零宽字符/BOM/控制符
    normalize_spaces: bool = True     # NBSP、连续空格、全角空格
    drop_noise_lines: bool = True     # 剔除已标记的页眉页脚/页码/版权行
    dehyphenate: bool = True          # 行末断词合并：com-\nputer → computer
    merge_lines: bool = True          # 断行合并成段落（保留段落边界）
    fix_citation_spacing: bool = True # disease[1] → disease [1]
    keep_noise_in_text: bool = False  # 调试用：清洗文本里也保留噪声行


RULE_DESC = {
    "fix_ligatures": "连字修复（ﬁ→fi 等）",
    "remove_control": "移除不可见控制符（软连字符/零宽字符）",
    "normalize_spaces": "空格规范化（NBSP/连续空格）",
    "drop_noise_lines": "剔除页眉页脚/页码/版权行",
    "dehyphenate": "行末断词合并（com- + puter → computer）",
    "merge_lines": "断行合并成段落",
    "fix_citation_spacing": "引用标记前补空格（disease[1] → disease [1]）",
}


# ---------------------------------------------------------------- 连字/字符映射
_LIGATURES = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
    "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st",
    "\u0152": "OE", "\u0153": "oe", "\u00c6": "AE", "\u00e6": "ae",
    "\u00df": "ss", "\u2010": "-", "\u2011": "-", "\u2012": "-",
    "\u2013": "-", "\u2014": "-", "\u2212": "-",  # 各种破折号/减号统一为 -
    "\u00a0": " ", "\u2007": " ", "\u202f": " ",  # 各种空格
}
_INVISIBLE = re.compile(r"[\u00ad\u200b\u200c\u200d\ufeff\u2060\u180e]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# ---------------------------------------------------------------- 行级清洗
def clean_line_text(text: str, cfg: CleanConfig) -> tuple[str, list[str]]:
    """单行文本清洗，返回 (新文本, 命中的规则名列表)。"""
    hits = []
    orig = text

    if cfg.fix_ligatures:
        t = text
        for k, v in _LIGATURES.items():
            t = t.replace(k, v)
        if t != text:
            hits.append("fix_ligatures")
            text = t

    if cfg.remove_control:
        t = _INVISIBLE.sub("", text)
        t = _CONTROL.sub("", t)
        if t != text:
            hits.append("remove_control")
            text = t

    if cfg.normalize_spaces:
        t = re.sub(r"[\s\u3000]{2,}", " ", text)          # 所有空白/全角空格折叠
        t = re.sub(r"\s+([,.;:)\]])", r"\1", t)   # 标点前多余空格
        t = re.sub(r"([(])\s+", r"\1", t)
        t = t.strip()
        if t != text:
            hits.append("normalize_spaces")
            text = t

    if cfg.fix_citation_spacing:
        t = re.sub(r"(\w)\[(\d)", r"\1 [\2", text)         # disease[1]
        t = re.sub(r"(\w)\((?=[A-Z][A-Za-z]+(?:\s+(?:et|and)\s+al\.?)?[,;]?\s*\d{4})", r"\1 (", text)
        if t != text:
            hits.append("fix_citation_spacing")
            text = t

    return text, hits


def clean_lines(lines: list[Line], cfg: CleanConfig) -> dict:
    """行级清洗：直接修改 line.text（line.raw_text 保留原始）。

    返回统计 {rule_name: 修改次数}。
    """
    stats: dict[str, int] = {}
    for ln in lines:
        new, hits = clean_line_text(ln.text, cfg)
        ln.text = new
        for h in hits:
            stats[h] = stats.get(h, 0) + 1
    return stats


# ---------------------------------------------------------------- 断词与段落重构
_HYPHEN_BREAK = re.compile(r"(\w)-\s*$")
_RE_SENT_END = re.compile(r"[.!?:][\"'”’\)\]]*\s*$")   # 句末标点（含后随引号/括号）
# 断词时保留连字符的例外：复合词前半（of-/well-…）与常见后半（-related/-based-）
_HYPHEN_KEEP_PREV = {"of", "to", "and", "or", "in", "on", "for", "the", "a", "an",
                     "with", "well", "long", "short", "low", "high", "self", "cross",
                     "half", "co", "quasi", "multi", "non", "pre", "post", "sub",
                     "inter", "intra", "anti", "pro", "semi", "over", "under"}
_HYPHEN_KEEP_NEXT = {"related", "based", "mediated", "induced", "dependent", "associated",
                     "like", "specific", "free", "term", "cell", "type", "model", "driven",
                     "responsive", "activity", "known", "state", "art", "old", "new",
                     "term", "cycle", "phase", "level", "positive", "negative"}
_HEADING_LINE = re.compile(r"^\s*(\d+(\.\d+){0,3}|[IVXLC]+\.|[A-Z]\.)\s+\S")


def _looks_like_paragraph_start(text: str) -> bool:
    """下一行是否像新段落/标题/列表/图表标题的开头。"""
    t = text.strip()
    if not t:
        return True
    if re.match(r"^(Figure|Fig\.|Table|Tab\.|Scheme|Chart|Scheme)\s*[S0-9IVX]", t, re.I):
        return True
    if re.match(r"^(\d+(\.\d+)*\.|[IVXLC]+\.|[A-Z]\.)\s+", t):
        return True
    if re.match(r"^[\u2022\u25cf\u25aa\-\*•]\s+", t):
        return True
    return False


def build_paragraphs(lines: list[Line], cfg: CleanConfig,
                    skip_idx: set | None = None) -> list[dict]:
    """把连续的正文行合并成段落。返回 [{text, page_start, page_end, line_start, line_end}]。

    合并决策（解决"完整语句被空格/换行断开"）：
      * 同一文本块内的行必然同段；跨块时看行距/缩进/栏位/句意连续性。
      * 行距明显变大（典型行距的 2.2 倍以上）或首行缩进 → 分段。
      * 句意未完（行尾无句末标点 + 下一行小写/括号/数字）→ 一定接上。
      * skip_idx 中的行（被抽走的图表内容）视为不存在：**不打断段落**。
    """
    skip_idx = skip_idx or set()

    def is_noise(ln):
        return ln.noise and cfg.drop_noise_lines and not cfg.keep_noise_in_text

    kept = [i for i, ln in enumerate(lines)
            if i not in skip_idx and ln.text.strip() and not is_noise(ln)]
    if not kept:
        return []

    # 典型行距：同文本块相邻行的垂直间距中位数
    gaps = []
    for a, b in zip(kept, kept[1:]):
        la, lb = lines[a], lines[b]
        if la.block and la.block == lb.block and la.page == lb.page:
            g = lb.y0 - la.y1
            if 0 <= g < 20:
                gaps.append(g)
    gaps.sort()
    typ = gaps[len(gaps) // 2] if gaps else 0.0

    def sentence_continues(pt: str, ct: str) -> bool:
        if re.search(r"[,;:(\[]\s*$", pt):
            return True
        if _RE_SENT_END.search(pt):
            return False
        return bool(re.match(r"^[a-z(\[\d]", ct)) or bool(re.match(r"^[A-Z]", ct))

    def between_artificial(pi: int, ci: int) -> bool:
        """两行之间是否只有被抽走的图表/噪声内容（人工间隔，不应切断上下文）。"""
        for k in range(pi + 1, ci):
            if k in skip_idx:
                continue
            ln = lines[k]
            if ln.text.strip() and not (ln.noise and cfg.drop_noise_lines and not cfg.keep_noise_in_text):
                return False
        return True

    def decide(pi: int, ci: int) -> str:
        """返回 join / nospace / dehyphen / keephyphen / break。"""
        pl, cl = lines[pi], lines[ci]
        pt, ct = pl.text.strip(), cl.text.strip()

        # 1) 行末断词：数字-数字保连字符（21- 23 → 21-23）；复合词保；普通词去
        m = _HYPHEN_BREAK.search(pt) if cfg.dehyphenate else None
        if m and re.match(r"^[a-z(\[\d]", ct) and not _looks_like_paragraph_start(ct):
            if m.group(1).isdigit() and ct[0].isdigit():
                return "keephyphen"
            if re.match(r"^[a-z]", ct):
                bw = re.search(r"([A-Za-z]+)-\s*$", pt)
                bw = bw.group(1).lower() if bw else ""
                fw = re.match(r"([a-z]+)", ct)
                fw = fw.group(1).lower() if fw else ""
                if bw in _HYPHEN_KEEP_PREV or fw in _HYPHEN_KEEP_NEXT:
                    return "keephyphen"
                return "dehyphen"

        # 2) 标题/图注/列表开头 → 分段
        if _looks_like_paragraph_start(ct):
            return "break"

        # 3) 特例：']' 紧接 '(' 不加空格（[5, 43](Fig. 2)）
        if pt.endswith("]") and ct.startswith("("):
            return "nospace"

        # 4) 跨栏：句意未完才接上，否则分段（图表抽离造成的人工间隔除外）
        art = between_artificial(pi, ci)
        if not art and pl.column and cl.column and pl.column != cl.column:
            return "join" if sentence_continues(pt, ct) else "break"

        # 5) 行距明显变大 / 首行缩进 / 换页 → 倾向分段（句意未完仍接）
        same_page = pl.page == cl.page
        big = (not art) and same_page and (cl.y0 - pl.y1) > max(3.0, typ * 2.2)
        indent = (not art) and same_page and (cl.x0 > pl.x0 + 3)
        if (big or indent or not same_page) and not sentence_continues(pt, ct):
            return "break"

        # 6) 同块 / 句意未完 / 紧密排列 → 接上（消除"语句被空格断开"）
        return "join"

    paras: list[dict] = []
    buf: list[str] = []
    start = None
    prev_i = None

    def flush(end_idx: int, page: int):
        nonlocal buf, start
        if buf and start is not None:
            text = re.sub(r"\s{2,}", " ", "".join(buf)).strip()
            if text:
                paras.append({
                    "text": text,
                    "page_start": lines[start].page,
                    "page_end": page,
                    "line_start": start,
                    "line_end": end_idx,
                })
        buf = []
        start = None

    for i in kept:
        ln = lines[i]
        t = ln.text.strip()
        if prev_i is None:
            buf = [t]
            start = i
            prev_i = i
            continue
        d = decide(prev_i, i)
        if d == "break":
            flush(prev_i, lines[prev_i].page)
            buf = [t]
            start = i
        elif d == "nospace":
            buf[-1] = buf[-1] + t
        elif d == "dehyphen":
            buf[-1] = re.sub(r"-\s*$", "", buf[-1]) + t
        elif d == "keephyphen":
            buf[-1] = buf[-1] + t
        else:
            buf.append(" " + t)
        prev_i = i

    flush(prev_i, lines[prev_i].page)
    return paras


def clean_full_text(lines: list[Line], cfg: CleanConfig) -> str:
    """整篇清洗文本：噪声行剔除 + 行拼接（简单按换行保留，用于导出 TXT）。"""
    out = []
    for ln in lines:
        if ln.noise and cfg.drop_noise_lines and not cfg.keep_noise_in_text:
            continue
        if ln.text.strip():
            out.append(ln.text.strip())
    return "\n".join(out)
