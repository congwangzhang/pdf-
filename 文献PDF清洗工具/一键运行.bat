@echo off
title 文献PDF清洗工具
chcp 936 >nul
cd /d "%~dp0"
echo [%date% %time%] 启动 > 启动日志.txt
echo 本脚本所在文件夹: "%~dp0" >> 启动日志.txt

echo ========================================
echo   文献PDF清洗工具 - 启动中...
echo ========================================
echo.

where python >> 启动日志.txt 2>&1
if errorlevel 1 goto nopython

python --version >> 启动日志.txt 2>&1
if errorlevel 1 goto nopython

python -c "import tkinter" >> 启动日志.txt 2>&1
if errorlevel 1 goto notk

python -c "import pymupdf" >> 启动日志.txt 2>&1
if errorlevel 1 goto install

goto launch

:install
echo [1/2] 首次运行，正在安装 PDF 引擎（约 1 分钟，请耐心等待）...
python -m pip install pymupdf pdfminer.six -q -i https://pypi.tuna.tsinghua.edu.cn/simple >> 启动日志.txt 2>&1
if errorlevel 1 python -m pip install pymupdf pdfminer.six -q >> 启动日志.txt 2>&1
python -c "import pymupdf" >> 启动日志.txt 2>&1
if errorlevel 1 goto installfail

:launch
echo [2/2] 正在打开图形界面，请看新弹出的窗口操作...
echo.
python main_gui.py >> 启动日志.txt 2>&1
echo.
echo 程序已退出。如果刚才没有看到界面窗口，请把本文件夹里的 启动日志.txt 发给 AI。
pause
exit /b 0

:nopython
echo.
echo [提示] 本机还没有安装 Python，工具无法运行。
echo.
echo   解决办法（只做一次，约 3 分钟）:
echo     1. 打开网站: https://www.python.org/downloads/
echo     2. 点黄色按钮下载，然后运行安装包
echo     3. [重要] 务必勾选安装页底部的 "Add Python to PATH"
echo     4. 装完后重新双击 一键运行.bat
echo.
pause
exit /b 1

:notk
echo.
echo [提示] Python 装好了，但缺少界面组件 tkinter。
echo   重新运行 Python 安装包，选择 Modify，勾选 tcl/tk and IDLE 即可。
echo.
pause
exit /b 1

:installfail
echo.
echo [提示] PDF 引擎安装失败。请检查网络后重新双击本脚本；
echo   若反复失败，请把 启动日志.txt 发给 AI。
echo.
pause
exit /b 1
