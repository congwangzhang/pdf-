# -*- coding: utf-8 -*-
"""export.py — 结果导出：JSON / Markdown / 标题树 / 质检报告 / 批量汇总。

输出目录结构（每篇文献一个文件夹，原始 PDF 永不改动）：
  输出目录/
    <文献名>/
      clean.json          结构化全量数据（四层合一：raw/clean/结构/质检）
      清洗文本.md          清洗后的全文（Markdown：标题层级/图注/表格/参考文献格式化）
      原始文本.txt         原始提取全文（含噪声行，供对照/回退）
      标题树.md            各级标题树（构建综述大纲的参照）
      质检报告.md
    汇总_标题树.md         所有文献的标题树合集
    汇总_文献清单.csv      一览表
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()[:120]


def _md_table(rows: list[list[str]]) -> str:
    """把行列数据转成 Markdown 表格。"""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    norm = [(r + [""] * (width - len(r))) for r in rows]
    def esc(c):
        return (c or "").replace("|", "\\|").replace("\n", " ").strip()
    out = ["| " + " | ".join(esc(c) for c in norm[0]) + " |",
           "|" + "---|" * width]
    for r in norm[1:]:
        out.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(out)


def document_markdown(result: dict) -> str:
    """把清洗结果渲染成一篇可读的 Markdown 文档（用户要求的 .md 交付格式）。"""
    md: list[str] = []
    if result.get("title"):
        md.append(f"# {result['title']}\n")
    if result.get("abstract"):
        md.append("## Abstract\n")
        md.append(result["abstract"] + "\n")
    if result.get("keywords"):
        md.append("**Keywords:** " + "; ".join(result["keywords"]) + "\n")

    md.append("---\n")
    for sec in result.get("sections", []):
        level = min(max(sec.get("level", 1), 1), 5)
        hdr = f"{sec.get('number', '')} {sec.get('heading', '')}".strip()
        md.append("#" * (level + 1) + " " + hdr + "\n")
        for p in sec.get("paragraphs", []):
            md.append(p.get("text", "") + "\n")

    refs = result.get("references", [])
    if refs:
        md.append("---\n")
        md.append("## References\n")
        for i, r in enumerate(refs, 1):
            text = re.sub(r"^\[\d{1,3}\]\s*", "", r.get("text", ""))
            md.append(f"{i}. {text}\n")

    # 图表内容统一放在文章最后，避免截断正文上下文
    figs = result.get("figures", [])
    if figs:
        md.append("---\n")
        md.append("## Figure Legends\n")
        for f in figs:
            md.append(f"> **{f['id']}.** {f['caption']}\n")

    tabs = result.get("tables", [])
    if tabs:
        md.append("---\n")
        md.append("## Tables\n")
        for t in tabs:
            md.append(f"**{t['id']}.** {t['caption']}\n")
            if t.get("rows"):
                tbl = _md_table(t["rows"])
                if tbl:
                    md.append(tbl + "\n")
            elif t.get("content"):
                md.append("```\n" + t["content"] + "\n```\n")
    return "\n".join(md)


def headings_markdown(result: dict, doc_name: str) -> str:
    out = [f"# 标题树：{doc_name}", ""]
    if result.get("title"):
        out += [f"**题目**：{result['title']}", ""]
    heads = result.get("headings", [])
    if not heads:
        out.append("（未识别到标题）")
        return "\n".join(out)
    out += ["| 层级 | 编号 | 标题 | 页码 | 状态 |", "|---|---|---|---|---|"]
    for h in heads:
        indent = "&nbsp;&nbsp;&nbsp;&nbsp;" * (h["level"] - 1)
        out.append(f"| {h['level']} | {h.get('number','')} | {indent}{h['text']} "
                   f"| p{h['page']} | {h.get('confirmed','')} |")
    out += ["", "## 大纲视图", ""]
    for h in heads:
        pad = "  " * (h["level"] - 1)
        num = (h.get("number") or "") + " "
        out.append(f"{pad}- {num}{h['text']}")
    return "\n".join(out)


def export_document(result: dict, quality: dict, out_dir: str | Path,
                    doc_name: str, source_file: str = "") -> Path:
    """导出单篇文献的全部产物，返回该文献的输出文件夹。"""
    out_dir = Path(out_dir)
    d = out_dir / _safe_name(doc_name)
    d.mkdir(parents=True, exist_ok=True)

    payload = dict(result)
    payload["source_file"] = source_file
    payload["quality"] = quality
    # clean/raw 文本在 JSON 里保留（体量可控，方便追溯）
    (d / "clean.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "清洗文本.md").write_text(document_markdown(result), encoding="utf-8")
    (d / "原始文本.txt").write_text(result.get("raw_text", ""), encoding="utf-8")
    (d / "标题树.md").write_text(headings_markdown(result, doc_name), encoding="utf-8")

    from .quality import report_markdown
    (d / "质检报告.md").write_text(report_markdown(doc_name, quality), encoding="utf-8")
    return d


def export_summary(results: list[tuple[str, dict, dict]], out_dir: str | Path) -> None:
    """批量汇总：results = [(doc_name, result, quality), ...]"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) 汇总标题树
    md = ["# 汇总标题树（所有文献）", "",
          "> 用途：为你的综述构建各级标题参照。", ""]
    for name, res, q in results:
        md.append(f"## {name}")
        if res.get("title"):
            md.append(f"*{res['title']}*")
        md.append("")
        for h in res.get("headings", []):
            pad = "  " * (h["level"] - 1)
            num = (h.get("number") or "") + " "
            flag = " ⚠️" if h.get("confirmed") == "待确认" else ""
            md.append(f"{pad}- {num}{h['text']}{flag}")
        md.append("")
    (out_dir / "汇总_标题树.md").write_text("\n".join(md), encoding="utf-8")

    # 2) 文献清单 CSV
    with open(out_dir / "汇总_文献清单.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["文献", "题目", "关键词", "标题数", "待确认标题", "表格", "图片",
                    "参考文献", "页数", "质检状态"])
        for name, res, q in results:
            st = q.get("stats", {})
            w.writerow([
                name,
                res.get("title", ""),
                "; ".join(res.get("keywords", [])),
                st.get("headings", 0),
                st.get("pending_headings", 0),
                st.get("tables", 0),
                st.get("figures", 0),
                st.get("references", 0),
                st.get("pages", 0),
                q.get("status", ""),
            ])
