#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

PID_FILE="data/bot.pid"
LOG_FILE="bridge.log"

if [[ -f "$PID_FILE" ]]; then
  existing_pid="$(tr -d '[:space:]' < "$PID_FILE")"
  if [[ -n "${existing_pid:-}" ]] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "Bot is already running with PID $existing_pid"
    exit 1
  fi
  rm -f "$PID_FILE"
fi

nohup python3 -u main.py >> "$LOG_FILE" 2>&1 &
new_pid=$!
sleep 2

if kill -0 "$new_pid" 2>/dev/null; then
  echo "Started bot with PID $new_pid"
  exit 0
fi

echo "Bot failed to start. Check $LOG_FILE"
exit 1
