# -*- coding: utf-8 -*-
"""classify.py — 结构化分类：题目 / 摘要 / 关键词 / 标题树 / 表图 / 参考文献。

标题识别原则（来自需求文档）：
  * 不只靠编号，也不只靠字体 —— 编号模式 + 字号 + 加粗 + 短行特征 综合判断。
  * 层级：有编号按编号（1 / 1.1 / 1.1.1）；无编号按字号聚类。
  * 拿不准的输出"待确认"，不强行分级。

噪声处理原则：
  * 版权许可 / 通讯作者 / 单位脚注 / 声明区（Ethics、Competing interests、
    Author details 等）→ 按**文本块**整体标记剔除（块内所有行），不留在正文。
  * 图表标题与 legend 按**文本块**整体抽离，不插在正文中间；
    正文段落跨过它们继续合并，保证上下文连贯。
  * 表格优先用 PyMuPDF 表格检测还原行列；不可靠时保留原始行文本。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from .extract import Extraction, Line
from .clean import CleanConfig, build_paragraphs, clean_lines


# ---------------------------------------------------------------- 正则
_RE_NUMBERED = re.compile(r"^\s*(\d+(\.\d+){0,4})\.?\s+(?=\S)")
_RE_ROMAN = re.compile(r"^\s*([IVXLC]{1,6})\.\s+(?=\S)")
_RE_ALPHA = re.compile(r"^\s*([A-Z])\.\s+(?=\S)")
_RE_CAPTION = re.compile(
    r"^\s*(Figure|Fig\.?|Table|Tab\.?|Scheme|Chart|Exhibit)[\s.]+([S0-9IVX]{1,5})"
    r"[\s.:—-]*(.*)$", re.I)
# 图注后面通常接描述（大写开头）；正文引用如 "Fig. 1 shows..." 接的是小写动词
_CAPTION_VERBS = re.compile(
    r"^(shows?|showed|illustrates?|demonstrates?|depicts?|indicates?|presents?|"
    r"is|are|was|were|can|cannot|we|our|the results|compares?)\b", re.I)
_RE_ABSTRACT = re.compile(r"^\s*(abstract|summary|overview)\s*$", re.I)
_RE_ABSTRACT_INLINE = re.compile(r"^\s*(abstract|summary)\b[\s.:—-]+\S", re.I)
_RE_KEYWORDS = re.compile(r"^\s*(key\s*words|keywords|index\s*terms|key\s*terms)\s*[:：]", re.I)
_RE_INTRO = re.compile(r"^\s*(\d+(\.\d+)*\.?\s+)?(introduction|background)\s*$", re.I)
_RE_REFERENCES_HEAD = re.compile(r"^\s*(references|literature cited|bibliography)\s*$", re.I)
_RE_REF_ENTRY = re.compile(r"^\s*(\[\d{1,3}\]|\d{1,3}\.?\s+[A-Z]|\[\d{1,3}-\d{1,3}\])")
_RE_HAS_YEAR = re.compile(r"\b(19|20)\d{2}[a-z]?\b|et\s+al|\[\d")
_RE_SENT_END = re.compile(r"[.!?:][\"'”’\)\]]*\s*$")

# ---- 噪声识别：版权许可 / 作者信息 / 声明区 ----
_RE_LICENSE = re.compile(
    r"(creative commons|creativecommons\.org|open access this article|©\s*the author|"
    r"licensed under|permitted by statutory regulation|public domain dedication|"
    r"the author\(s\) 20|reprints and permissions|rights reserved|"
    r"this is a pdf file of an unedited manuscript|accepted manuscript|"
    r"downloaded from|published by springer|bmc part of springer nature)", re.I)
_RE_AUTHORINFO = re.compile(
    r"(^\*?\s*correspondence\s*:|corresponding author|contributed equally|"
    r"full list of author information|^\s*[†‡*]+\s*$|orcid\.org|"
    r"^\s*\*?\s*e-?mail\s*:|^\s*present address|^\s*(received|accepted|published online)\s*:)", re.I)
_RE_AFFILIATION = re.compile(
    r"^\s*\d{0,2}\s*(department|dept\.?|institute|school|college|hospital|"
    r"center|centre|university|laboratory|faculty|academy|university|"
    r"biotherapy|cancer center|key laboratory)\b", re.I)
_RE_BOILER_HEAD = re.compile(
    r"^\s*(author details|authors?'? contributions?|ethics (approval|declarations?)|"
    r"consent for publication|competing interests|conflicts? of interest|"
    r"publisher'?s note|availability of data( and materials)?|data availability|"
    r"supplementary information|rights and permissions|declarations?|"
    r"abbreviations?|acknowledg(e)?ments?|funding|grant support)\s*\.?\s*$", re.I)

# 常见章节名（用于无编号标题的兜底识别）
_CANON_HEADS = re.compile(
    r"^\s*(introduction|background|related work|literature review|state of the art|state-of-the-art|"
    r"methods?|methodology|materials and methods|experimental(\s+(setup|section))?|"
    r"results?|results and discussion|discussion|conclusions?|conclusion and future|"
    r"future (directions?|work|perspectives?|outlook)|challenges?|opportunities|"
    r"limitations?|applications?|advances?|recent advances|taxonomy|classification|"
    r"acknowledg(e)?ments?|funding|outlook|perspectives?)\s*$", re.I)


def _word_count(t: str) -> int:
    return len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", t))


# ---------------------------------------------------------------- 字号基准
def estimate_body_size(lines: list[Line], skip_idx: set | None = None) -> float:
    """正文字号 = 按字符数加权的众数（排除噪声、图注/表格、题目行）。"""
    skip_idx = skip_idx or set()
    counter: Counter = Counter()
    for i, ln in enumerate(lines):
        if i in skip_idx or ln.noise or len(ln.text.strip()) < 25 or ln.size <= 0:
            continue
        counter[round(ln.size * 2) / 2] += len(ln.text)
    if not counter:
        return 10.0
    return counter.most_common(1)[0][0]


# ---------------------------------------------------------------- 文本块工具
def _block_extent(lines: list[Line], i: int) -> tuple[int, int]:
    """行 i 所属文本块的 [起, 止] 行号（block 标签相同且连续）。"""
    blk = lines[i].block
    if not blk:
        return i, i
    a = i
    while a > 0 and lines[a - 1].block == blk:
        a -= 1
    b = i
    while b + 1 < len(lines) and lines[b + 1].block == blk:
        b += 1
    return a, b


def _block_lines(lines: list[Line], i: int) -> tuple[list[int], int, int]:
    a, b = _block_extent(lines, i)
    return list(range(a, b + 1)), a, b


# ---------------------------------------------------------------- 噪声块标记
def mark_noise_blocks(lines: list[Line], front: dict) -> int:
    """版权许可、通讯作者/单位脚注、声明区 → 按块整体标记 noise。返回标记行数。"""
    marked = 0
    abs_start = None
    if front.get("abstract_range"):
        abs_start = front["abstract_range"][0]
    title_idx = set(front.get("title_lines") or [])

    seen_blocks = set()
    for i, ln in enumerate(lines):
        if ln.noise or not ln.text.strip():
            continue
        if ln.block and ln.block in seen_blocks:
            continue
        idxs, a, b = _block_lines(lines, i)
        if ln.block:
            seen_blocks.add(ln.block)
        first = lines[a].text.strip()
        head2 = " ".join(x.text.strip() for x in lines[a:min(b, a + 1) + 1])
        in_front = abs_start is not None and b < abs_start

        reason = ""
        if _RE_LICENSE.search(first) or _RE_LICENSE.search(head2[:300]):
            reason = "版权/许可声明"
        elif _RE_AUTHORINFO.search(first):
            reason = "作者/通讯信息"
        elif in_front and not (set(idxs) & title_idx):
            # 摘要之前的其余块（作者、单位、脚注、通讯）整体剔除
            if _RE_AFFILIATION.search(first) or len(first) < 200:
                reason = "作者/单位信息"
        if reason:
            for j in idxs:
                if not lines[j].noise:
                    lines[j].noise = reason
                    marked += 1

    # 声明区（Ethics/Competing interests/Author details 等成段剔除）
    i = 0
    while i < len(lines):
        t = lines[i].text.strip()
        if not lines[i].noise and t and _RE_BOILER_HEAD.match(t) and _word_count(t) <= 6:
            j = i
            while j < len(lines):
                t2 = lines[j].text.strip()
                if j > i and t2 and (
                        _RE_BOILER_HEAD.match(t2) or _RE_REFERENCES_HEAD.match(t2)
                        or _RE_NUMBERED.match(t2) or _CANON_HEADS.match(t2)
                        or _RE_ABSTRACT.match(t2) or _RE_CAPTION.match(t2)):
                    break
                if not lines[j].noise:
                    lines[j].noise = "声明/无用信息"
                    marked += 1
                j += 1
            i = j
        else:
            i += 1
    return marked


# ---------------------------------------------------------------- 标题识别
@dataclass
class Heading:
    id: str
    level: int
    number: str
    text: str
    page: int
    order: int
    parent_id: str | None
    line_idx: int = -1
    inline: bool = False       # 段首斜体短语：同段剩余文字仍是正文
    confirmed: str = "auto"     # auto / 待确认
    basis: str = ""             # 识别依据


def detect_headings(lines: list[Line], body_size: float, skip_ranges: list[tuple],
                    skip_idx: set | None = None,
                    page_widths: dict | None = None) -> tuple[list[Heading], dict]:
    """识别各级标题。skip_ranges: [(行号起, 行号止)]；skip_idx: 跳过的具体行号。

    无编号综述（模式 C）层级规则：字号 > 字体（Bold > Italic > Regular）；
    段首斜体短语 = 三级标题；标题一般 ≤ 10 词。
    """
    skip_idx = skip_idx or set()
    page_widths = page_widths or {}

    def skipped(i):
        return i in skip_idx or any(a <= i <= b for a, b in skip_ranges)

    # 行间垂直间隙（上下文信号）：标题上方通常有明显空行
    gap_above: dict[int, float] = {}
    typical_gap: dict[int, float] = {}
    by_page: dict[int, list[int]] = defaultdict(list)
    for i, ln in enumerate(lines):
        by_page[ln.page].append(i)
    for pg, idxs in by_page.items():
        gaps = []
        for a, b in zip(idxs, idxs[1:]):
            g = lines[b].y0 - lines[a].y1
            if 0 < g < 60:
                gap_above[b] = g
                gaps.append(g)
        if gaps:
            gs = sorted(gaps)
            typical_gap[pg] = gs[len(gs) // 2]

    cands = []
    anchor_styles: dict[tuple, int] = {}   # 锚点标题（编号/常见章节名）的样式 → 层级
    n_lines = len(lines)

    def next_real(idx: int):
        """下一个未被跳过/非噪声的行。"""
        for k in range(idx + 1, min(idx + 6, n_lines)):
            if lines[k].noise or skipped(k) or not lines[k].text.strip():
                continue
            return lines[k]
        return None

    def followed_by_paragraph(idx: int, self_key: tuple) -> bool:
        """标题后面应是正文段落（长行）；若后面还是零碎短行 → 表格/数据区。"""
        for k in range(idx + 1, min(idx + 6, n_lines)):
            lnk = lines[k]
            if lnk.noise or skipped(k) or not lnk.text.strip():
                continue
            if (round(lnk.size * 2) / 2, lnk.bold, lnk.italic) == self_key:
                continue          # 同样式的标题续行
            t2 = lnk.text.strip()
            if _word_count(t2) < 5 and not (_RE_NUMBERED.match(t2) or _CANON_HEADS.match(t2)):
                return False
            return True
        return True

    for i, ln in enumerate(lines):
        if ln.noise or skipped(i) or not ln.text.strip():
            continue

        # ---- 模式 C 三级标题：段首斜体短语（短语后同段还有正文）----
        ph = (ln.lead_italic or "").strip()
        if ph and 1 <= _word_count(ph) <= 10 and ln.text.startswith(ph[:12]):
            mm = re.match(re.escape(ph) + r"[\s.:—-]*", ln.text)
            rest = ln.text[mm.end():].strip() if mm else ""
            phrase = re.sub(r"[.:—-]+$", "", ph).strip()
            if phrase and rest and _word_count(rest) >= 3:
                cands.append([i, 3, "", phrase, "段首斜体", False, False, True])
                ln.text = rest          # 短语归标题，剩余文字留在正文
                continue

        t = ln.text.strip()
        wc = _word_count(t)
        if wc > 14 or len(t) > 110:
            continue          # 综述标题一句话不会太长
        is_phrase = bool((ln.italic or ln.lead_italic) and wc <= 10 and re.search(r"[.:—-]$", t))
        if not is_phrase and not re.search(r"[A-Za-z0-9\u4e00-\u9fff]$", t):
            continue          # 标题通常以字母/数字结尾（段首斜体短语除外）
        if _RE_CAPTION.match(t):
            continue
        if _RE_HAS_YEAR.search(t):
            continue          # 含年份/et al./引用标的的是文献条目或正文，不是标题
        # 作者行片段：断行后的 “Liu et” / 逗号分隔的作者列表
        if re.search(r"\bet\s*$", t) or re.match(r"^([A-Z][\w'.-]+,\s*){1,5}[A-Z][\w'.-]+$", t):
            continue

        # 所在文本块的总词数：正文长句被换行拆短后也不应误判为标题
        blk_idxs, ba, bb = _block_lines(lines, i)
        block_wc = _word_count(" ".join(lines[k].text for k in blk_idxs))

        level, number, basis = 0, "", ""
        is_cont = False
        m = _RE_NUMBERED.match(t)
        if m:
            num = m.group(1)
            rest = t[m.end():]
            numeric_ok = not re.fullmatch(r"\d{3,4}", num)
            next_cap = bool(re.match(r"^[A-Z(]", rest))
            if not (numeric_ok and next_cap):
                m = None
        if m:
            number = m.group(1)
            level = number.count(".") + 1
            basis = "编号模式"
            t = t[m.end():].strip()
        else:
            m2 = _RE_ROMAN.match(t)
            m3 = _RE_ALPHA.match(t)
            if m2 and wc <= 8 and block_wc <= 12:
                number, level, basis = m2.group(1), 1, "罗马编号"
                t = t[m2.end():].strip()
            elif m3 and wc <= 8 and block_wc <= 12:
                number, level, basis = m3.group(1), 2, "字母编号"
                t = t[m3.end():].strip()
            elif _RE_ABSTRACT.match(t) or _RE_KEYWORDS.match(t):
                continue
            elif _CANON_HEADS.match(t) and block_wc <= 12:
                level, basis = 1, "常见章节名"
            else:
                # 无编号标题：字号偏大 / 加粗 / 斜体，且必须像标题而不像正文/数据
                styled = (ln.size >= body_size + 0.5) or ((ln.bold or ln.italic) and ln.size >= body_size - 0.5)
                if not styled or block_wc > 25 or wc < 2 or wc > 10:
                    continue          # 综述标题一般 ≤ 10 词
                # 上下文信号：标题上方通常有空行（间隙明显大于正文行距）或居中。
                # 字号明显大于正文的强候选不卡（部分排版标题紧贴上文）；
                # 同字号加粗/斜体的弱候选必须满足，防段内加粗短语误判。
                pg = ln.page
                tg = typical_gap.get(pg, 0.0)
                gap_ok = (tg <= 0) or (gap_above.get(i, 0.0) >= tg * 1.1)
                centered0 = False
                if page_widths:
                    w0 = page_widths.get(pg, 595.0)
                    centered0 = abs((ln.x0 + ln.x1) / 2 - w0 / 2) < 15
                size_standout = ln.size >= body_size + 0.5
                if not size_standout and not (gap_ok or centered0):
                    continue          # 段落中间冒出来的加粗短语不是标题
                if is_phrase:
                    # 段首斜体短语（模式 C 三级标题）：整行即标题
                    t = re.sub(r"[.:—-]+$", "", t).strip()
                    level, basis = 0, "字号/样式"
                    is_cont = False
                    cands.append([i, level, "", t, basis, True, False, False])
                    key = (round(ln.size * 2) / 2, ln.bold, ln.italic)
                    continue
                is_cont = not re.match(r"^[A-Z(\[]", t)
                if not is_cont:
                    key_self = (round(ln.size * 2) / 2, ln.bold, ln.italic)
                    # 居中标题位置信号强，免验“后接正文段落”
                    centered = False
                    if page_widths:
                        w = page_widths.get(ln.page, 595.0)
                        centered = abs((ln.x0 + ln.x1) / 2 - w / 2) < 15
                    if not centered and not followed_by_paragraph(i, key_self):
                        continue
                level, basis = 0, "字号/样式"
        if level == 0 and not basis:
            continue
        needs = (basis == "字号/样式")
        cands.append([i, level, number, t, basis, needs, is_cont, False])
        # 锚点样式指纹：编号/常见章节名标题的 (字号, 加粗, 斜体) 代表该文档的标题样式
        if basis in ("编号模式", "罗马编号", "字母编号", "常见章节名"):
            key = (round(ln.size * 2) / 2, ln.bold, ln.italic)
            if key not in anchor_styles or level < anchor_styles[key]:
                anchor_styles[key] = level

    if not cands:
        return [], {}

    merged = []
    for c in cands:
        if c[7]:
            merged.append({"idx": c[0], "idx_end": c[0], "level": c[1], "number": c[2],
                           "text": c[3], "basis": c[4], "needs": c[5], "inline": True})
            continue
        if c[6] and not merged:
            continue          # 孤立的标题续行 → 丢弃
        if merged:
            prev = merged[-1]
            gap = c[0] - prev["idx_end"]
            if c[6]:
                # 小写开头的“标题续行”：只能并入前一个标题，否则丢弃
                if prev["needs"] and gap <= 2 and not _RE_SENT_END.search(prev["text"]):
                    prev["text"] = prev["text"] + " " + c[3]
                    prev["idx_end"] = c[0]
                continue
            if (gap <= 1 and prev["needs"] and c[5]
                    and not _RE_SENT_END.search(prev["text"])
                    and not c[2] and not _RE_NUMBERED.match(c[3])):
                prev["text"] = prev["text"] + " " + c[3]
                prev["idx_end"] = c[0]
                continue
        merged.append({
            "idx": c[0], "idx_end": c[0], "level": c[1], "number": c[2],
            "text": c[3], "basis": c[4], "needs": c[5], "inline": False,
        })

    # 无编号标题层级：字号 > 字体（Bold > Italic > Regular）
    style_keys = sorted(
        {(round(lines[m["idx"]].size * 2) / 2, lines[m["idx"]].bold, lines[m["idx"]].italic)
         for m in merged if not m["number"] and m["basis"] == "字号/样式"},
        key=lambda k: (-k[0], 0 if k[1] else (1 if k[2] else 2)))
    style_levels = {k: i + 1 for i, k in enumerate(style_keys[:4])}
    size_levels = {k[0]: v for k, v in style_levels.items()}

    headings: list[Heading] = []
    stack: dict[int, str] = {}
    for n, m in enumerate(merged):
        if m.get("inline"):
            level = 3
        elif m["number"]:
            level = min(m["level"], 4)
        else:
            key = (round(lines[m["idx"]].size * 2) / 2, lines[m["idx"]].bold, lines[m["idx"]].italic)
            if m["basis"] == "字号/样式" and key in anchor_styles:
                level = anchor_styles[key]        # 与锚点同样式 → 同层级
            else:
                level = style_levels.get(key, 1) or 1
        parent = None
        for lv in range(level - 1, 0, -1):
            if lv in stack:
                parent = stack[lv]
                break
        hid = f"H{n + 1:03d}"
        headings.append(Heading(
            id=hid, level=level, number=m["number"], text=m["text"],
            page=lines[m["idx"]].page, order=n + 1, parent_id=parent,
            line_idx=m["idx"], inline=m.get("inline", False),
            confirmed="待确认" if m["needs"] and not m["number"] else "auto",
            basis=m["basis"],
        ))
        stack[level] = hid
        for lv in list(stack):
            if lv > level:
                del stack[lv]

    meta = {"body_size": body_size, "size_levels": size_levels}
    return headings, meta


# ---------------------------------------------------------------- 题录区识别
def find_front_matter(lines: list[Line], body_size: float) -> dict:
    """识别题目、摘要、关键词。返回各部分行号区间。"""
    info = {"title": "", "title_lines": [], "abstract": "",
            "keywords": [], "abstract_range": None, "keywords_line": None,
            "front_lines": [], "pre_abstract_range": None}

    abs_start = abs_end = None
    kw_line = None
    for i, ln in enumerate(lines):
        if ln.noise or not ln.text.strip():
            continue
        t = ln.text.strip()
        if abs_start is None and (_RE_ABSTRACT.match(t) or _RE_ABSTRACT_INLINE.match(t)):
            abs_start = i
        if kw_line is None and _RE_KEYWORDS.match(t):
            kw_line = i
            break
        if abs_start is not None and kw_line is None and _RE_INTRO.match(t):
            abs_end = i - 1
            break

    if abs_start is not None:
        if kw_line is not None:
            abs_end = kw_line - 1
        elif abs_end is None:
            abs_end = abs_start + 40
        abs_end = max(abs_end, abs_start)
        parts = []
        for i in range(abs_start, abs_end + 1):
            t = lines[i].text.strip()
            if i == abs_start:
                t = _RE_ABSTRACT_INLINE.sub("", t)
                t = _RE_ABSTRACT.sub("", t)
            if t:
                parts.append(t)
        info["abstract"] = " ".join(parts).strip()
        info["abstract_range"] = (abs_start, abs_end)

    if kw_line is not None:
        t = lines[kw_line].text.strip()
        t = _RE_KEYWORDS.sub("", t)
        kws = [k.strip() for k in re.split(r"[;,;•|]|,(?=\s)", t) if k.strip()]
        info["keywords"] = kws
        info["keywords_line"] = kw_line

    last_front = abs_start if abs_start is not None else min(len(lines) - 1, 60)
    page1 = [(i, ln) for i, ln in enumerate(lines[:last_front])
             if not ln.noise and ln.text.strip() and ln.page == 1]
    if page1:
        max_size = max(ln.size for _, ln in page1)
        title_lines = []
        for i, ln in page1:
            if ln.size >= max_size - 0.5 and ln.size >= body_size:
                title_lines.append((i, ln))
            elif title_lines:
                break
        if title_lines:
            info["title"] = " ".join(l.text.strip() for _, l in title_lines)
            info["title_lines"] = [i for i, _ in title_lines]
        info["front_lines"] = list(range(0, last_front))
    if abs_start is not None:
        info["pre_abstract_range"] = (0, abs_start - 1)
    return info


# ---------------------------------------------------------------- 图表标题与内容
def _block_looks_tabular(lines: list[Line], idxs: list[int]) -> bool:
    """文本块是否像表格内容（单元格行，而非连续散文）。"""
    ws = [_word_count(lines[j].text) for j in idxs if lines[j].text.strip()]
    if not ws:
        return False
    long_prose = any(w >= 18 for w in ws)
    tabular_spans = 0
    for j in idxs:
        raw = lines[j].raw_text or lines[j].text
        if len(re.findall(r"\s{2,}|\t", raw)) >= 2:
            tabular_spans += 1
    short_lines = sum(1 for w in ws if w <= 10) / len(ws)
    return (not long_prose) and (tabular_spans >= 1 or short_lines >= 0.7)


def find_captions(lines: list[Line], body_size: float,
                  page_tables: dict | None = None) -> tuple[list[dict], list[dict], set]:
    """识别 Figure/Table 标题与 legend（按文本块整体抽离），并抓取表格内容。

    返回 (figures, tables, consumed_idx)。
    """
    page_tables = page_tables or {}
    figures, tables = [], []
    consumed: set[int] = set()
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if ln.noise or i in consumed:
            i += 1
            continue
        m = _RE_CAPTION.match(ln.text.strip())
        if not m:
            i += 1
            continue
        rest = m.group(3).strip()
        # "Fig. 1 shows..." 是正文引用，不是图注
        if rest and _CAPTION_VERBS.match(rest) and not re.match(r"^[A-Z(]", rest):
            i += 1
            continue

        kind = "figure" if re.match(r"(figure|fig\.?|scheme|chart|exhibit)", m.group(1), re.I) else "table"
        num = m.group(2)

        # 图注/legend = 整个文本块；表标题块内可能混着表格行 → 在首个单元格行切开
        idxs, a, b = _block_lines(lines, i)
        cap_idxs = []
        row_start = None
        for jx in idxs:
            if jx < i:
                continue
            if cap_idxs and kind == "table" and _word_count(lines[jx].text) <= 3:
                row_start = jx          # 表格数据行开始
                break
            cap_idxs.append(jx)
        cap_done = bool(_RE_SENT_END.search(lines[cap_idxs[-1]].text.strip() or "x"))
        j = b + 1
        merged = 0
        if row_start is None:
            while (not cap_done) and merged < 2 and j < n and not lines[j].noise and lines[j].text.strip():
                nidxs, na, nb = _block_lines(lines, j)
                if na != j:
                    break
                if _RE_CAPTION.match(lines[j].text.strip()) or _RE_NUMBERED.match(lines[j].text.strip()):
                    break
                txt = " ".join(lines[k].text.strip() for k in nidxs)
                if _word_count(txt) > 40 or _block_looks_tabular(lines, nidxs):
                    break
                cap_idxs.extend(nidxs)
                merged += 1
                j = nb + 1
                cap_done = bool(_RE_SENT_END.search(txt))

        caption = " ".join(lines[k].text.strip() for k in cap_idxs)
        for k in cap_idxs:
            consumed.add(k)

        if kind == "figure":
            figures.append({"id": f"F{num}", "caption": caption, "page": ln.page})
            i = max(j, i + 1)
            continue

        # 表格内容：优先结构化表格检测（find_tables），否则收集表格样式文本块
        rows = None
        content_lines = []
        content_idx: set[int] = set()
        best = None
        cands = page_tables.get(ln.page, [])
        if cands:
            best_d = 1e9
            for t in cands:
                ty0, ty1 = t["bbox"][1], t["bbox"][3]
                d = (ty0 - ln.y1) if ty0 >= ln.y0 else (ln.y0 - ty1)
                if -20 <= d < 400 and d < best_d:
                    best, best_d = t, d
            if best and any(any(c for c in r) for r in best["rows"]):
                rows = best["rows"]
        if rows is not None:
            # 表格区域内的文字行全部归入表格，从正文剔除
            bx0, by0, bx1, by1 = best["bbox"]
            for k2, ln2 in enumerate(lines):
                if ln2.page != ln.page or ln2.noise or k2 in consumed:
                    continue
                cy = (ln2.y0 + ln2.y1) / 2
                cx = (ln2.x0 + ln2.x1) / 2
                if by0 - 2 <= cy <= by1 + 2 and bx0 - 2 <= cx <= bx1 + 2:
                    consumed.add(k2)
                    content_idx.add(k2)
        else:
            # 表标题块内混着的行
            if row_start is not None:
                for x in range(row_start, b + 1):
                    content_lines.append((lines[x].raw_text or lines[x].text).rstrip())
                    consumed.add(x)
                    content_idx.add(x)
            k = max(j, b + 1)
            while k < n and k not in consumed:
                if lines[k].noise or not lines[k].text.strip():
                    k += 1
                    continue
                nidxs, na, nb = _block_lines(lines, k)
                if na != k:
                    k += 1
                    continue
                if _RE_CAPTION.match(lines[k].text.strip()) or _RE_NUMBERED.match(lines[k].text.strip()):
                    break
                if _block_looks_tabular(lines, nidxs):
                    for x in nidxs:
                        content_lines.append((lines[x].raw_text or lines[x].text).rstrip())
                        consumed.add(x)
                        content_idx.add(x)
                    k = nb + 1
                    if len(content_lines) > 80:
                        break
                else:
                    break
        tables.append({"id": f"T{num}", "caption": caption, "page": ln.page,
                       "rows": rows, "content": "\n".join(content_lines),
                       "_content_lines": content_idx})
        i = max(j, i + 1)

    # 兕底：find_tables 检测到但未被任何表标题关联的表格（无标题表/跨页续表）
    # → 行归入表格条目，避免单元格漏进正文/标题
    for pg in sorted(page_tables.keys()):
        for t in page_tables[pg]:
            bx0, by0, bx1, by1 = t["bbox"]
            inside = [k for k, ln in enumerate(lines)
                      if ln.page == pg and not ln.noise
                      and bx0 - 2 <= (ln.x0 + ln.x1) / 2 <= bx1 + 2
                      and by0 - 2 <= (ln.y0 + ln.y1) / 2 <= by1 + 2]
            if not inside or all(k in consumed for k in inside):
                continue
            if not any(any((c or "").strip() for c in r) for r in t["rows"]):
                continue
            tid = f"T?{pg}-{len(tables) + 1}"
            tables.append({"id": tid, "caption": "", "page": pg,
                           "rows": t["rows"], "content": "",
                           "_content_lines": set(inside)})
            for k in inside:
                consumed.add(k)
    return figures, tables, consumed


def drop_tiny_tables(tables: list[dict]) -> tuple[list[dict], set]:
    """剔除“只有一两行”的假表格（正常表格不可能只有一两行）。
    返回 (保留的表格, 被剔除表格占用的行号集合)。"""
    keep, dropped = [], set()
    for t in tables:
        rows = t.get("rows")
        if rows is not None:
            n = sum(1 for r in rows if any((c or "").strip() for c in r))
        else:
            n = len([l for l in (t.get("content") or "").splitlines() if l.strip()])
        if n <= 2:
            dropped.update(t.get("_content_lines", set()))
            continue
        keep.append(t)
    return keep, dropped


# ---------------------------------------------------------------- 参考文献
_RE_REF_LIKE = re.compile(r"\[\d{1,3}\]|\b(19|20)\d{2}[a-z]?\b|et al", re.I)


def _collect_refs(lines: list[Line], start: int) -> tuple[list[dict], bool]:
    """从 start 行之后收集参考文献条目，并校验这真是参考文献区。"""
    refs: list[dict] = []
    buf: list[str] = []
    page = 0

    def flush():
        nonlocal buf, page
        if buf:
            refs.append({"text": " ".join(buf).strip(), "page": page})
        buf = []

    for ln in lines[start + 1:]:
        t = ln.text.strip()
        if ln.noise or not t:
            continue
        if _RE_REF_ENTRY.match(t):
            flush()
            buf = [t]
            page = ln.page
        elif buf:
            buf.append(t)
    flush()

    # 校验：真实参考文献几乎都含年份 / et al.；表格数据行不满足
    good = sum(1 for r in refs[:8] if re.search(r"\b(19|20)\d{2}[a-z]?\b", r["text"])
               or re.search(r"et\s+al", r["text"], re.I))
    ok = len(refs) >= 2 and good >= 2
    return refs, ok


def find_references(lines: list[Line], skip_idx: set | None = None) -> tuple[list[dict], tuple | None]:
    """定位参考文献区。防误判：
    1) 表格里的列头“References”不认（skip_idx 中的行跳过）；
    2) 必须是独立的小文本块；
    3) 后面必须真的跟着带年份的文献条目；
    4) 多个候选取最后一个（参考文献总在文末）。
    """
    skip_idx = skip_idx or set()
    cands = []
    for i, ln in enumerate(lines):
        if ln.noise or i in skip_idx or not ln.text.strip():
            continue
        if not _RE_REFERENCES_HEAD.match(ln.text.strip()):
            continue
        idxs, a, b = _block_lines(lines, i)
        if _word_count(" ".join(lines[k].text for k in idxs)) > 6:
            continue
        cands.append(i)

    for start in reversed(cands):
        refs, ok = _collect_refs(lines, start)
        if ok:
            return refs, (start, len(lines) - 1)
    return [], None


# ---------------------------------------------------------------- 主流程
def classify_document(ext: Extraction, cfg: CleanConfig,
                      page_tables: dict | None = None) -> dict:
    """提取 → 清洗 → 分类 的完整流程，返回结构化字典（可直接存 JSON）。"""
    lines = ext.lines

    line_stats = clean_lines(lines, cfg)

    # 顺序：题录（初判）→ 图表抽离 → 正文字号基准 → 题录（精判）→
    #        噪声块标记 → 参考文献 → 标题 → 分节
    front_a = find_front_matter(lines, 10.0)
    figures, tables, cap_consumed = find_captions(lines, 10.0, page_tables)
    # 假表格（只有一两行）剔除；其内容行若不被其它表格占用，释放回正文
    tables, tiny_rows = drop_tiny_tables(tables)
    claimed = set()
    for t in tables:
        claimed |= t.get("_content_lines", set())
    cap_consumed -= (tiny_rows - claimed)
    for t in tables:
        t.pop("_content_lines", None)
    est_skip = set(cap_consumed) | set(front_a.get("title_lines") or [])
    body_size = estimate_body_size(lines, est_skip)

    front = find_front_matter(lines, body_size)
    noise_extra = mark_noise_blocks(lines, front)
    refs, ref_range = find_references(lines, skip_idx=cap_consumed)

    skip_idx: set[int] = set(cap_consumed)
    skip = []
    if front["abstract_range"]:
        skip.append(front["abstract_range"])
    if front["pre_abstract_range"]:
        skip.append(front["pre_abstract_range"])
    if front["keywords_line"] is not None:
        skip_idx.add(front["keywords_line"])
    if front["title_lines"]:
        skip_idx.update(front["title_lines"])
    if ref_range:
        skip.append(ref_range)

    headings, hmeta = detect_headings(
        lines, body_size, skip, skip_idx,
        page_widths={p.number: p.width for p in ext.pages})

    h_line_idx = [(h.line_idx, h) for h in headings if h.line_idx >= 0]
    marks = sorted(h_line_idx, key=lambda x: x[0])

    consumed_rows = set(cap_consumed)
    if front["abstract_range"]:
        consumed_rows.update(range(front["abstract_range"][0], front["abstract_range"][1] + 1))
    if front["keywords_line"] is not None:
        consumed_rows.add(front["keywords_line"])
    if front["title_lines"]:
        consumed_rows.update(front["title_lines"])
    if ref_range:
        consumed_rows.update(range(ref_range[0], ref_range[1] + 1))

    def collect_body(start: int, end: int) -> list[Line]:
        out = []
        for i in range(start, min(end + 1, len(lines))):
            if i in consumed_rows:
                continue
            ln = lines[i]
            if ln.noise or not ln.text.strip():
                continue
            if any(i == mi and not getattr(h, "inline", False) for mi, h in marks):
                continue
            out.append(ln)
        return out

    sections = []
    if marks:
        bounds = [(marks[k][0], (marks[k + 1][0] - 1) if k + 1 < len(marks) else len(lines) - 1)
                  for k in range(len(marks))]
        for (s, e), (_, h) in zip(bounds, marks):
            body_lines = collect_body(s, e)
            paras = build_paragraphs(body_lines, cfg)
            sections.append({
                "id": f"S{h.order:03d}",
                "heading_id": h.id,
                "heading": h.text,
                "level": h.level,
                "number": h.number,
                "page_start": h.page,
                "paragraphs": paras,
            })
    else:
        body_lines = collect_body(0, len(lines) - 1)
        paras = build_paragraphs(body_lines, cfg)
        if paras:
            sections.append({"id": "S000", "heading_id": "", "heading": "(未识别标题)",
                             "level": 1, "number": "", "page_start": 1, "paragraphs": paras})

    # 清洗文本 = 段落级重构后的连贯正文（跨图表块不断裂）
    parts = []
    if front["title"]:
        parts.append(front["title"])
    if front["abstract"]:
        parts.append(front["abstract"])
    if front["keywords"]:
        parts.append("Keywords: " + "; ".join(front["keywords"]))
    for sec in sections:
        hdr = f"{sec['number']} {sec['heading']}".strip()
        parts.append(hdr)
        for p in sec["paragraphs"]:
            parts.append(p["text"])
    clean_text = "\n\n".join(parts)

    return {
        "engine": ext.engine,
        "page_count": ext.page_count,
        "title": front["title"],
        "abstract": front["abstract"],
        "keywords": front["keywords"],
        "headings": [vars(h) for h in headings],
        "heading_meta": hmeta,
        "sections": sections,
        "tables": tables,
        "figures": figures,
        "references": refs,
        "clean_text": clean_text,
        "raw_text": ext.raw_text(),
        "line_clean_stats": line_stats,
        "noise_extra": noise_extra,
        "warnings": ext.warnings,
    }
