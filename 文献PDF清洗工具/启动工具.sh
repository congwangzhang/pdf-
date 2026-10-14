#!/usr/bin/env bash
# 文献PDF清洗工具 · Mac/Linux 启动脚本
cd "$(dirname "$0")"
echo "[$(date)] 文献PDF清洗工具启动" > 启动日志.txt

if ! command -v python3 >/dev/null 2>&1; then
  echo "没有找到 python3。Mac 请安装：xcode-select --install"
  read -r -p "按回车退出..."
  exit 1
fi

python3 -c "import tkinter" >> 启动日志.txt 2>&1
if [ $? -ne 0 ]; then
  echo "Python 缺少 tkinter 界面组件。Mac 自带 Python 一般没问题；"
  echo "若用 Homebrew Python：brew install python-tk"
  read -r -p "按回车退出..."
  exit 1
fi

python3 -c "import pymupdf" >> 启动日志.txt 2>&1
if [ $? -ne 0 ]; then
  echo "正在安装 PDF 引擎，请稍候..."
  python3 -m pip install pymupdf pdfminer.six -q >> 启动日志.txt 2>&1
fi

python3 main_gui.py >> 启动日志.txt 2>&1
echo "程序退出。如遇问题请把 启动日志.txt 发给 AI。"
read -r -p "按回车退出..."
