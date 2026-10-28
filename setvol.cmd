@echo off
chcp 65001 >nul
rem setvol [0.0..1.0] [--device pickle|mini2]
python "%~dp0set_volume.py" %*
