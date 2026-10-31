@echo off
title PG Companion
cd /d "%~dp0"
py -m pip install --quiet --disable-pip-version-check -r requirements.txt
py server.py
pause
