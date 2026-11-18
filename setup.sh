#!/usr/bin/env bash
# clip-watcher setup for macOS / Linux: finds or installs Python 3.10-3.13 and ffmpeg,
# then installs the Python libraries into .venv. Asks before installing anything.
#   ./setup.sh        (add --yes to install without asking)
set -euo pipefail
cd "$(dirname "$0")"
YES=0; [[ "${1:-}" == "--yes" ]] && YES=1

ask() { [[ $YES == 1 ]] && return 0; read -r -p "$1 [Y/n] " a; [[ -z "$a" || "$a" =~ ^[yY] ]]; }

install_pkg() {  # install_pkg <brew name> <apt/dnf/pacman names...>
  local brew_name=$1; shift
  if [[ "$(uname)" == "Darwin" ]]; then
    if ! command -v brew >/dev/null; then
      echo "Homebrew is needed to install $brew_name: https://brew.sh - then run setup again."; exit 1
    fi
    brew install "$brew_name"
  elif command -v apt-get >/dev/null; then sudo apt-get update && sudo apt-get install -y "$@"
  elif command -v dnf >/dev/null; then sudo dnf install -y "$@"
  elif command -v pacman >/dev/null; then sudo pacman -S --noconfirm "$@"
  else echo "Please install $* with your package manager, then run setup again."; exit 1
  fi
}

find_python() {
  for p in python3.12 python3.13 python3.11 python3.10 python3; do
    if command -v "$p" >/dev/null && "$p" -c 'import sys; sys.exit(not (3,10) <= sys.version_info[:2] <= (3,13))' 2>/dev/null; then
      echo "$p"; return 0
    fi
  done
  return 1
}

echo "=== clip-watcher setup ==="
if ! PY=$(find_python); then
  echo "Python 3.10-3.13 was not found."
  ask "Install Python 3.12 now?" || { echo "Install Python 3.12 from https://www.python.org/downloads/ and run setup again."; exit 1; }
  install_pkg python@3.12 python3 python3-venv python3-pip
  PY=$(find_python) || { echo "Python still not found - open a new terminal and run setup again."; exit 1; }
fi
echo "[ok] Python: $(command -v "$PY")"

if ! command -v ffmpeg >/dev/null; then
  echo "ffmpeg was not found."
  ask "Install ffmpeg now?" || { echo "Install ffmpeg (https://ffmpeg.org/download.html) and run setup again."; exit 1; }
  install_pkg ffmpeg ffmpeg
fi
echo "[ok] ffmpeg: $(command -v ffmpeg)"

[[ -x .venv/bin/python ]] || "$PY" -m venv .venv
echo "Installing Python libraries (first time takes a few minutes)..."
.venv/bin/python -m pip install --disable-pip-version-check -q --upgrade pip
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
.venv/bin/python -c "import streamlink, faster_whisper, cv2, numpy"
echo "[ok] Python libraries"
chmod +x clip.sh 2>/dev/null || true
echo "Setup complete. Try:  ./clip.sh <twitch channel> --vertical --burn"
