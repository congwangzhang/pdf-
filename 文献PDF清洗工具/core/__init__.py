# -*- coding: utf-8 -*-
"""core — 文献PDF清洗工具 · 核心逻辑（不含界面，可独立测试）。

流程：提取(extract) → 行级清洗(clean) → 结构分类(classify) →
      段落重构 → 质检(quality) → 导出(export)

数据分层（重要）：
  raw_text   原始提取文本，永不修改
  clean_text 规则清洗后的文本
  changelog  每条规则的修改记录
"""
__version__ = "1.0.0"
CLEAN_VERSION = 1
