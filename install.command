#!/bin/bash
echo "========================================"
echo "  Simple Video Transcriber — Installer"
echo "========================================"
echo ""

# ── Homebrew ──
if ! command -v brew &> /dev/null; then
    echo "Homebrew not found. Installing..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    if [ $? -ne 0 ]; then
        echo "ERROR: Could not install Homebrew."
        echo "Please install it manually: https://brew.sh"
        exit 1
    fi
    # Add brew to PATH for this session
    eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv 2>/dev/null)"
fi

# ── Python ──
if ! command -v python3 &> /dev/null; then
    echo "Python not found. Installing..."
    brew install python@3.12
fi
python3 --version
echo ""

# ── ffmpeg ──
if ! command -v ffmpeg &> /dev/null; then
    echo "ffmpeg not found. Installing..."
    brew install ffmpeg
else
    echo "ffmpeg found."
fi
echo ""

# ── Python dependencies ──
echo "Installing Python packages..."
pip3 install -r "$(dirname "$0")/requirements.txt"

echo ""
echo "========================================"
echo "  Installation complete!"
echo ""
echo "  Double-click start.command to launch"
echo "  Simple Video Transcriber."
echo "========================================"
echo ""
read -p "Press Enter to close..."
