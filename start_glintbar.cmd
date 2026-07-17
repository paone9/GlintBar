@echo off
rem Launches GlintBar from source with no console window (for environments that block .vbs).
cd /d "%~dp0"
start "" pythonw -m glintbar
