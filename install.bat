@echo off
title Simple Video Transcriber — Installer
echo.
echo ========================================
echo   Simple Video Transcriber — Installer
echo ========================================
echo.

:: ── Python ──
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python not found. Installing via winget...
    winget install Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    if %errorlevel% neq 0 (
        echo.
        echo ERROR: Could not install Python automatically.
        echo Please install Python 3.10+ from https://python.org and run this script again.
        pause
        exit /b 1
    )
    echo Python installed. Please close and re-open this window, then run install.bat again.
    pause
    exit /b
)
python --version
echo.

:: ── ffmpeg ──
where ffmpeg >nul 2>nul
if %errorlevel% neq 0 (
    echo ffmpeg not found. Installing via winget...
    winget install Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements
    if %errorlevel% neq 0 (
        echo.
        echo WARNING: Could not install ffmpeg automatically.
        echo Please install ffmpeg from https://ffmpeg.org/download.html
        echo and add it to your PATH.
    ) else (
        echo ffmpeg installed.
    )
) else (
    echo ffmpeg found.
)
echo.

:: ── Python dependencies ──
echo Installing Python packages...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo WARNING: Some packages may have failed to install.
    echo Try running: pip install -r requirements.txt
)

echo.
echo ========================================
echo   Installation complete!
echo.
echo   Double-click start.bat to launch
echo   Simple Video Transcriber.
echo ========================================
echo.
pause
