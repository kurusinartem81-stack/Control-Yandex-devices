@echo off
chcp 65001 >nul
rem probe [--device pickle|mini2]
python "%~dp0probe.py" %*
