@echo off
rem double-click to open the trading GUI
cd /d "%~dp0"
set UV_LINK_MODE=copy
uv run gui.py
