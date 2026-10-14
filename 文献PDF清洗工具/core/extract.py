# -*- coding: utf-8 -*-
"""extract.py — PDF 原始内容提取（保留字体、字号、坐标、栏位信息）。

原则：
  * 尽量保留结构信息：每行文字都带 bbox / 字号 / 是否加粗 / 所在栏。
  * 双栏排版：按"全宽行分段 + 左栏→右栏"重建阅读顺序。
  * 页眉页脚：跨页重复 + 位置 + 正则 三重判断，只做**标记**不直接删除。
  * 只读原始 PDF，绝不写回。

引擎：PyMuPDF(fitz) 优先；缺失时降级 pdfminer.six（功能略减）。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------- 数据结构
@dataclass
class Line:
    """一行文字（提取后的最小处理单元）。"""
    page: int                 # 页码（从 1 开始）
    order: int                # 阅读顺序（页内）
    text: str                 # 原始文字
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    size: float = 0.0         # 字号（按字符长度加权平均）
    bold: bool = False
    italic: bool = False
    lead_italic: str = ""     # 段首斜体短语（模式 C 三级标题），如 "Senescence."
    font: str = ""
    column: int = 0           # 0=全宽 1=左栏 2=右栏
    block: str = ""           # 所属文本块 ID（同一段落/图注的行共享）
    noise: str = ""           # 非空表示疑似噪声，值为原因，如 "页眉"/"页码"
    raw_text: str = ""        # 提取后未清洗的文字（行级清洗前的备份）

    @property
    def bbox(self):
        return [self.x0, self.y0, self.x1, self.y1]


@dataclass
class PageInfo:
    number: int
    width: float
    height: float
    has_text_layer: bool
    char_count: int
    scanned: bool = False     # 疑似扫描页（文字极少）


@dataclass
class Extraction:
    """一篇 PDF 的原始提取结果。"""
    source: str
    engine: str
    pages: list = field(default_factory=list)       # list[PageInfo]
    lines: list = field(default_factory=list)       # list[Line]
    warnings: list = field(default_factory=list)
    page_count: int = 0
    encrypted: bool = False

    def raw_text(self) -> str:
        """原始层文本：按页拼接，噪声行也保留。"""
        out = []
        for p in range(1, self.page_count + 1):
            out.append(f"\n===== [PAGE {p}] =====")
            for ln in self.lines:
                if ln.page == p:
                    out.append(ln.text)
        return "\n".join(out)


# ---------------------------------------------------------------- 引擎探测
def _can_import(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


HAS_FITZ = _can_import("fitz")
HAS_PDFMINER = _can_import("pdfminer.high_level")


def engine_status() -> str:
    eng = []
    if HAS_FITZ:
        eng.append("PyMuPDF(推荐)")
    if HAS_PDFMINER:
        eng.append("pdfminer(降级)")
    return " + ".join(eng) if eng else "无可用引擎！请安装 pymupdf 或 pdfminer.six"


# ---------------------------------------------------------------- 双栏阅读顺序
def _order_lines(raw_lines: list[Line], page_width: float) -> list[Line]:
    """把一页的行按阅读顺序排序：全宽行作为分段点，段内先左栏后右栏。"""
    if not raw_lines:
        return []
    mid = page_width / 2.0
    tol = page_width * 0.12  # 超过这个范围算真正的全宽行

    full, left, right = [], [], []
    for ln in raw_lines:
        w = ln.x1 - ln.x0
        center = (ln.x0 + ln.x1) / 2.0
        is_full = ln.x0 < mid - tol and ln.x1 > mid + tol
        # 很窄的行（标题、短句）按中心位置归栏
        if is_full or w > page_width * 0.62:
            ln.column = 0
            full.append(ln)
        elif center < mid:
            ln.column = 1
            left.append(ln)
        else:
            ln.column = 2
            right.append(ln)

    full.sort(key=lambda l: (l.y0, l.x0))
    left.sort(key=lambda l: (l.y0, l.x0))
    right.sort(key=lambda l: (l.y0, l.x0))

    if not left and not right:
        ordered = full
    else:
        # 以全宽行的中线为界切纵向区间，区间无缝覆盖全部 y（不丢行）
        cuts = [(l.y0 + l.y1) / 2.0 for l in full]

        def zone_lines(lo: float, hi: float):
            zl = [l for l in left if lo <= (l.y0 + l.y1) / 2.0 < hi]
            zr = [l for l in right if lo <= (l.y0 + l.y1) / 2.0 < hi]
            return zl + zr

        ordered = []
        prev = -1e9
        for f, c in zip(full, cuts):
            ordered.extend(zone_lines(prev, c))
            ordered.append(f)
            prev = c
        ordered.extend(zone_lines(prev, 1e9))

    for i, ln in enumerate(ordered):
        ln.order = i
    return ordered


# ---------------------------------------------------------------- 页眉页脚识别
_RE_PAGE_NO = re.compile(r"^\s*page\s*\d+(\s*(of|/)\s*\d+)?\s*$", re.I)
_RE_PURE_NO = re.compile(r"^\s*[-–—(]?\d{1,4}[)–—-]?\s*$")
_RE_DOI = re.compile(r"(doi\s*:?\s*10\.|https?://|www\.)", re.I)
_RE_COPY = re.compile(r"(©|copyright|all rights reserved|elsevier|springer|wiley|taylor & francis|"
                      r"american chemical society|ieee|mdpi|frontiers media|downloaded from|"
                      r"accepted manuscript|published by|this is a pdf file)", re.I)
_RE_VOLPAGES = re.compile(r"^\s*[A-Za-z&.\s]{3,40}\s+\d+(\(\d+\))?\s*\(\d{4}\)\s*[\dx]+[-–][\dx]+\s*$")


def _norm_key(text: str) -> str:
    """页眉页脚重复比对用的归一化键：去掉数字、空白、标点，转小写。"""
    return re.sub(r"[\d\s\W_]+", "", text, flags=re.U).lower()[:60]


def detect_headers_footers(lines: list[Line], pages: list[PageInfo]) -> int:
    """跨页重复 + 位置 + 正则，标记疑似页眉页脚/页码/版权行。返回标记数量。

    只做标记（line.noise），不删除 —— 由后续清洗层决定是否剔除。
    """
    n_pages = len(pages) or 1
    height = {p.number: p.height for p in pages} or {}
    marked = 0

    # 位置候选：每页顶部 9% / 底部 9% 区域
    top_keys, bot_keys = Counter(), Counter()
    for ln in lines:
        h = height.get(ln.page, 792.0)
        key = _norm_key(ln.text)
        if not key:
            continue
        if ln.y1 <= h * 0.10:
            top_keys[key] += 1
        elif ln.y0 >= h * 0.90:
            bot_keys[key] += 1

    repeat_thresh = max(2, int(n_pages * 0.3))
    repeat_top = {k for k, c in top_keys.items() if c >= repeat_thresh}
    repeat_bot = {k for k, c in bot_keys.items() if c >= repeat_thresh}

    for ln in lines:
        if ln.noise:
            continue
        h = height.get(ln.page, 792.0)
        key = _norm_key(ln.text)
        t = ln.text.strip()
        reason = ""
        if key and ((ln.y1 <= h * 0.10 and key in repeat_top) or
                    (ln.y0 >= h * 0.90 and key in repeat_bot)):
            reason = "页眉/页脚(跨页重复)"
        elif _RE_PAGE_NO.match(t) or (_RE_PURE_NO.match(t) and (ln.y0 > h * 0.85 or ln.y1 < h * 0.12)):
            reason = "页码"
        elif _RE_VOLPAGES.match(t):
            reason = "卷期页码"
        elif _RE_DOI.search(t) and (ln.y1 <= h * 0.12 or ln.y0 >= h * 0.88 or len(t) < 90):
            reason = "DOI/网址"
        elif _RE_COPY.search(t) and (ln.y1 <= h * 0.14 or ln.y0 >= h * 0.86):
            reason = "版权/出版信息"
        if reason:
            ln.noise = reason
            marked += 1
    return marked


# ---------------------------------------------------------------- PyMuPDF 引擎
def _extract_fitz(path: Path, max_pages: int = 0) -> Extraction:
    try:
        import pymupdf as fitz          # 新版 API
    except ImportError:
        import fitz                     # 旧版兼容
    doc = fitz.open(str(path))
    ext = Extraction(source=str(path), engine="PyMuPDF", encrypted=bool(doc.needs_pass))
    if doc.needs_pass:
        ext.warnings.append("PDF 加密/需要密码，无法提取")
        doc.close()
        return ext

    n = doc.page_count if max_pages <= 0 else min(max_pages, doc.page_count)
    all_lines: list[Line] = []
    for pno in range(n):
        page = doc[pno]
        pw, ph = page.rect.width, page.rect.height
        d = page.get_text("dict")
        page_lines: list[Line] = []
        char_total = 0
        for bi, block in enumerate(d.get("blocks", [])):
            if block.get("type", 0) != 0:
                continue  # 图片块
            block_id = f"p{pno + 1}b{bi}"
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text_parts, sizes, fonts = [], [], []
                bold = False
                italic = False
                for sp in spans:
                    text_parts.append(sp.get("text", ""))
                    st = sp.get("size", 0.0)
                    sizes.append(st)
                    fn = sp.get("font", "")
                    fonts.append(fn)
                    flags = sp.get("flags", 0)
                    if flags & 16 or re.search(r"bold|black|heavy", fn, re.I):
                        bold = True
                    if flags & 2 or re.search(r"italic|oblique", fn, re.I):
                        italic = True
                text = "".join(text_parts)
                if not text.strip():
                    continue
                char_total += len(text)
                wsum = sum(max(len(t), 1) for t in text_parts)
                size = sum(s * max(len(t), 1) for s, t in zip(sizes, text_parts)) / max(wsum, 1)
                # 段首斜体短语：首 span 为斜体且后面还有非斜体内容（模式 C 三级标题）
                lead_italic = ""
                sp0 = spans[0]
                if (sp0.get("flags", 0) & 2 or re.search(r"italic|oblique", sp0.get("font", ""), re.I)):
                    t0 = sp0.get("text", "").strip()
                    later_regular = any(
                        not ((sp.get("flags", 0) & 2) or re.search(r"italic|oblique", sp.get("font", ""), re.I))
                        and sp.get("text", "").strip()
                        for sp in spans[1:])
                    if t0 and later_regular and len(t0.split()) <= 12:
                        lead_italic = t0
                x0, y0, x1, y1 = line.get("bbox", (0, 0, 0, 0))
                page_lines.append(Line(
                    page=pno + 1, order=0, text=text, raw_text=text,
                    x0=x0, y0=y0, x1=x1, y1=y1,
                    size=round(size, 1), bold=bold, italic=italic,
                    lead_italic=lead_italic,
                    font=fonts[0] if fonts else "",
                    block=block_id,
                ))
        page_lines = _order_lines(page_lines, pw)
        all_lines.extend(page_lines)
        ext.pages.append(PageInfo(
            number=pno + 1, width=pw, height=ph,
            has_text_layer=char_total > 20, char_count=char_total,
            scanned=char_total < 20,
        ))
    ext.page_count = n
    ext.lines = all_lines
    doc.close()

    scanned = [p.number for p in ext.pages if p.scanned]
    if scanned:
        ext.warnings.append(f"疑似扫描页(无文字层)：第 {scanned} 页 —— 需要 OCR，本版暂按空白处理")
    return ext


# ---------------------------------------------------------------- pdfminer 降级引擎
def _extract_pdfminer(path: Path, max_pages: int = 0) -> Extraction:
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer, LTTextLine, LTChar

    ext = Extraction(source=str(path), engine="pdfminer(降级)")
    all_lines: list[Line] = []
    try:
        for pno, page_layout in enumerate(extract_pages(str(path))):
            if max_pages > 0 and pno >= max_pages:
                break
            pw, ph = page_layout.width, page_layout.height
            page_lines: list[Line] = []
            char_total = 0
            for bi, el in enumerate(page_layout):
                if not isinstance(el, LTTextContainer):
                    continue
                block_id = f"p{pno + 1}b{bi}"
                for sub in el:
                    if not isinstance(sub, LTTextLine):
                        continue
                    text = sub.get_text()
                    if not text.strip():
                        continue
                    char_total += len(text)
                    sizes, fonts, bold, italic = [], [], False, False
                    for ch in getattr(sub, "_objs", []):
                        if isinstance(ch, LTChar):
                            sizes.append(ch.size)
                            fn = getattr(ch, "fontname", "") or ""
                            fonts.append(fn)
                            if re.search(r"bold|black|heavy", fn, re.I):
                                bold = True
                            if re.search(r"italic|oblique", fn, re.I):
                                italic = True
                    size = max(sizes) if sizes else 0.0
                    x0, y0, x1, y1 = sub.x0, sub.y0, sub.x1, sub.y1
                    # 段首斜体短语（字符级字体判断）
                    lead_italic = ""
                    chars = [ch for ch in getattr(sub, "_objs", []) if isinstance(ch, LTChar)]
                    seg = []
                    rest_chars = []
                    hit_regular = False
                    for ch in chars:
                        fn = (getattr(ch, "fontname", "") or "")
                        if not hit_regular and re.search(r"italic|oblique", fn, re.I):
                            seg.append(ch.get_text())
                        else:
                            hit_regular = True
                            rest_chars.append(ch)
                    t0 = "".join(seg).strip()
                    if t0 and rest_chars and len(t0.split()) <= 12:
                        lead_italic = t0
                    # pdfminer 的 y 轴自下而上，翻转到"从上往下"
                    y0, y1 = ph - sub.y1, ph - sub.y0
                    page_lines.append(Line(
                        page=pno + 1, order=0, text=text.strip(), raw_text=text.strip(),
                        x0=x0, y0=y0, x1=x1, y1=y1,
                        size=round(size, 1), bold=bold, italic=italic,
                        lead_italic=lead_italic,
                        font=fonts[0] if fonts else "",
                        block=block_id,
                    ))
            page_lines = _order_lines(page_lines, pw)
            all_lines.extend(page_lines)
            ext.pages.append(PageInfo(
                number=pno + 1, width=pw, height=ph,
                has_text_layer=char_total > 20, char_count=char_total,
                scanned=char_total < 20,
            ))
    except Exception as e:
        ext.warnings.append(f"pdfminer 提取异常：{e}")
    ext.page_count = len(ext.pages)
    ext.lines = all_lines
    scanned = [p.number for p in ext.pages if p.scanned]
    if scanned:
        ext.warnings.append(f"疑似扫描页(无文字层)：第 {scanned} 页 —— 需要 OCR，本版暂按空白处理")
    return ext


# ---------------------------------------------------------------- 对外接口
def extract_pdf(path: str | Path, max_pages: int = 0) -> Extraction:
    """提取 PDF 的全部行级内容（含字体/字号/坐标/栏位），并标记页眉页脚类噪声。

    max_pages: >0 时只处理前 N 页（用于快速试跑）。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if HAS_FITZ:
        ext = _extract_fitz(path, max_pages)
    elif HAS_PDFMINER:
        ext = _extract_pdfminer(path, max_pages)
    else:
        raise RuntimeError("缺少 PDF 引擎：请先安装 pymupdf（推荐）或 pdfminer.six")

    detect_headers_footers(ext.lines, ext.pages)
    return ext


def extract_page_tables(path: str | Path) -> dict:
    """用 PyMuPDF 的表格检测提取结构化表格：{页码: [{bbox, rows: [[cell,…],…]}]}。

    失败或引擎不支持时返回空字典（调用方降级为文本行收集）。
    """
    out: dict[int, list[dict]] = {}
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        doc = fitz.open(str(path))
        if doc.needs_pass:
            doc.close()
            return out
        for pno in range(doc.page_count):
            page = doc[pno]
            try:
                tf = page.find_tables()
            except Exception:
                continue
            tabs = []
            for t in getattr(tf, "tables", []):
                try:
                    rows = t.extract()
                    rows = [[(c or "").strip() for c in r] for r in rows]
                    tabs.append({"bbox": list(t.bbox), "rows": rows})
                except Exception:
                    continue
            if tabs:
                out[pno + 1] = tabs
        doc.close()
    except Exception:
        return {}
    return out


def file_hash(path: str | Path) -> str:
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
