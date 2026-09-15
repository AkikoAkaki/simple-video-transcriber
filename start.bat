@echo off
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0tray_app.py"
) else (
    for /f "delims=" %%P in ('python -c "import sys; from pathlib import Path; print(Path(sys.executable).with_name('pythonw.exe'))"') do start "" "%%P" "%~dp0tray_app.py"
)
