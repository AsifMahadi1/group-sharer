#!/usr/bin/env bash
# Group Sharer (bot + userbot) — crash-restart runner
cd "$(dirname "$0")"
if [ ! -s config.json ]; then
  echo "⚠️ config.json খালি — আগে চালান: python3 setup.py"
  exit 1
fi
while true; do
  .venv/bin/python -u bot.py >> bot.log 2>&1
  echo "[runner] bot exited (code $?), restarting in 5s..." >> bot.log
  sleep 5
done
