# -*- coding: utf-8 -*-
"""pipeline.py — 一条命令跑完：提取 → 清洗 → 分类 → 质检。"""
from __future__ import annotations

from pathlib import Path

from .extract import extract_pdf, file_hash, extract_page_tables
from .clean import CleanConfig
from .classify import classify_document
from .quality import quality_report


def process_pdf(path: str | Path, cfg: CleanConfig | None = None,
                max_pages: int = 0, progress=None) -> tuple[dict, dict]:
    """处理单篇 PDF，返回 (结构化结果, 质检报告)。

    progress: 可选回调 progress(msg:str)，用于界面显示阶段信息。
    """
    cfg = cfg or CleanConfig()
    path = Path(path)

    if progress:
        progress("提取 PDF（保留字体/坐标/栏位）…")
    ext = extract_pdf(path, max_pages=max_pages)
    noise = sum(1 for ln in ext.lines if ln.noise)

    if progress:
        progress("检测表格结构…")
    page_tables = extract_page_tables(path) if max_pages <= 0 else {}

    if progress:
        progress("清洗 + 结构分类…")
    result = classify_document(ext, cfg, page_tables)
    result["source_file"] = str(path)
    result["file_hash"] = file_hash(path)
    result["file_size"] = path.stat().st_size

    if progress:
        progress("质量检查…")
    quality = quality_report(result, noise + result.get("noise_extra", 0))
    return result, quality


def find_pdfs(folder: str | Path) -> list[Path]:
    folder = Path(folder)
    return sorted(p for p in folder.rglob("*.pdf")
                  if not p.name.startswith("~") and not p.name.startswith("."))
