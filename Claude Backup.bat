@echo off
cd /d "%~dp0"
py -3 -m claude_backup ui || python -m claude_backup ui
