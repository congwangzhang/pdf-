@echo off
title 环境诊断
chcp 936 >nul
cd /d "%~dp0"
echo ========================================
echo   运行环境诊断（结果同时写入 诊断报告.txt）
echo ========================================
echo.
echo [诊断] %date% %time% > 诊断报告.txt

echo 本文件夹: "%~dp0"
echo 本文件夹: "%~dp0" >> 诊断报告.txt

echo.
echo --- 1. Python 是否安装 ---
where python >> 诊断报告.txt 2>&1
if errorlevel 1 (
  echo [X] 没有找到 python
  echo [X] 没有找到 python >> 诊断报告.txt
) else (
  echo [OK] 找到 python:
  python --version
  python --version >> 诊断报告.txt 2>&1
)

echo.
echo --- 2. 界面组件 tkinter ---
python -c "import tkinter; print('tkinter OK')" 2>&1
python -c "import tkinter; print('tkinter OK')" >> 诊断报告.txt 2>&1

echo.
echo --- 3. PDF 引擎 ---
python -c "import pymupdf; print('pymupdf OK')" 2>&1
python -c "import pymupdf; print('pymupdf OK')" >> 诊断报告.txt 2>&1

echo.
echo --- 4. 工具文件是否齐全 ---
if exist main_gui.py (echo [OK] main_gui.py) else (echo [X] 缺少 main_gui.py —— 请把压缩包完整解压后再运行!)
if exist core (echo [OK] core 文件夹) else (echo [X] 缺少 core 文件夹 —— 请把压缩包完整解压后再运行!)
if exist main_gui.py (echo [OK] main_gui.py >> 诊断报告.txt) else (echo [X] 缺少 main_gui.py >> 诊断报告.txt)

echo.
echo ========================================
echo 诊断完成。请把窗口里的内容截图，或把 诊断报告.txt 发给 AI。
echo ========================================
pause
