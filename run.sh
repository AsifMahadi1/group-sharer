#!/usr/bin/env bash
# Run the group-sharer bot
cd "$(dirname "$0")"
if [ ! -s config.json ]; then
  echo "⚠️ config.json খালি — আগে চালান: python3 setup.py"
  exit 1
fi
exec python3 bot.py