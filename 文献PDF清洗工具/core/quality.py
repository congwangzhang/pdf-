# -*- coding: utf-8 -*-
"""quality.py — 质量检查与报告。

原则：清洗完成 ≠ 所有问题自动修复，而是所有问题都被修复 / 人工接受 / 明确标记待处理。
"""
from __future__ import annotations

import re
from collections import Counter


_GARBLED = re.compile(r"[\ufffd\u25a1\u25a0?�]|Ã[\x80-\xbf]")
_SUSPECT_HYPHEN = re.compile(r"\w-\s+\w")


def quality_report(result: dict, ext_lines_noise: int = 0) -> dict:
    """对 classify_document 的结果做质量统计。"""
    clean = result.get("clean_text", "")
    raw = result.get("raw_text", "")
    pages = result.get("page_count", 0)
    issues: list[dict] = []

    def add(level: str, kind: str, msg: str):
        issues.append({"level": level, "kind": kind, "message": msg})

    # 1) 乱码率
    if clean:
        garbled = len(_GARBLED.findall(clean))
        ratio = garbled / max(len(clean), 1)
        if ratio > 0.01:
            add("警告", "乱码", f"异常字符比例偏高：{ratio:.2%}（{garbled} 个）")
        elif garbled:
            add("提示", "乱码", f"发现 {garbled} 个疑似异常字符")
    else:
        add("错误", "提取", "清洗文本为空 —— 可能是扫描件（无文字层）或提取失败")

    # 2) 疑似扫描页
    for w in result.get("warnings", []):
        add("警告", "页面", w)

    # 3) 结构完整性
    if not result.get("title"):
        add("警告", "结构", "未识别到题目")
    if not result.get("abstract"):
        add("警告", "结构", "未识别到摘要（可能无 Abstract 标签）")
    if not result.get("keywords"):
        add("提示", "结构", "未识别到关键词")
    if not result.get("headings"):
        add("警告", "结构", "未识别到任何标题")
    pending = [h for h in result.get("headings", []) if h.get("confirmed") == "待确认"]
    if pending:
        add("提示", "标题", f"{len(pending)} 个标题层级由字号/加粗推断，建议人工确认")

    # 4) 疑似断词残留
    leftover = len(_SUSPECT_HYPHEN.findall(clean))
    if leftover > 20:
        add("提示", "断词", f"仍有 {leftover} 处疑似断词（含连字符+空格），可人工抽查")

    # 5) 段落异常
    secs = result.get("sections", [])
    short = sum(1 for s in secs for p in s.get("paragraphs", [])
                if len(p.get("text", "")) < 40)
    if short > 15:
        add("提示", "段落", f"{short} 个过短段落（<40字符），可能存在断行合并不足")

    # 6) 噪声剔除统计
    noise_removed = ext_lines_noise
    if noise_removed:
        add("提示", "噪声", f"标记并剔除 {noise_removed} 行页眉页脚/页码/版权类噪声")

    # 7) 覆盖情况
    if pages:
        add("提示", "覆盖", f"共处理 {pages} 页，"
                           f"{len(result.get('references', []))} 条参考文献，"
                           f"{len(result.get('tables', []))} 个表格标题，"
                           f"{len(result.get('figures', []))} 个图片标题")

    level_count = Counter(i["level"] for i in issues)
    status = "通过"
    if level_count.get("错误"):
        status = "失败"
    elif level_count.get("警告"):
        status = "有警告"

    return {
        "status": status,
        "issues": issues,
        "stats": {
            "pages": pages,
            "clean_chars": len(clean),
            "garbled": len(_GARBLED.findall(clean)) if clean else 0,
            "headings": len(result.get("headings", [])),
            "pending_headings": len(pending),
            "references": len(result.get("references", [])),
            "tables": len(result.get("tables", [])),
            "figures": len(result.get("figures", [])),
            "noise_lines": noise_removed,
        },
    }


def report_markdown(doc_name: str, quality: dict) -> str:
    st = quality["stats"]
    lines = [
        f"# 质检报告：{doc_name}",
        "",
        f"**状态：{quality['status']}**",
        "",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 处理页数 | {st['pages']} |",
        f"| 清洗文本字数 | {st['clean_chars']} |",
        f"| 疑似乱码字符 | {st['garbled']} |",
        f"| 识别标题数 | {st['headings']}（其中待确认 {st['pending_headings']}） |",
        f"| 参考文献条数 | {st['references']} |",
        f"| 表格标题 | {st['tables']} |",
        f"| 图片标题 | {st['figures']} |",
        f"| 剔除噪声行 | {st['noise_lines']} |",
        "",
        "## 问题清单",
        "",
    ]
    if quality["issues"]:
        for i in quality["issues"]:
            lines.append(f"- **[{i['level']}]**（{i['kind']}）{i['message']}")
    else:
        lines.append("- 无")
    lines += [
        "",
        "> 说明：'待确认'标题指系统按字号/加粗推断的标题，"
        "建议在工具的『标题树』页人工核对层级。",
    ]
    return "\n".join(lines)
