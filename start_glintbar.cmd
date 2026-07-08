@echo off
rem Launches glintbar with no console window (for environments that block .vbs).
cd /d "%~dp0"
start "" pythonw monitor.py
