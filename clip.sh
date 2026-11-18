#!/usr/bin/env bash
# Usage: ./clip.sh <channel> [options]    e.g.  ./clip.sh summit1g --vertical --burn
DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ ! -x "$DIR/.venv/bin/python" ]]; then
  echo "First run - setting things up..."
  bash "$DIR/setup.sh" || exit 1
fi
exec "$DIR/.venv/bin/python" "$DIR/clip_watcher.py" "$@"
