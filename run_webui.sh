#!/usr/bin/env bash
# Group Sharer WebUI starter (local admin panel on 127.0.0.1:8017)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f data.db ]; then
  echo "data.db পাওয়া যায়নি — আগে বট চালিয়ে ডেটাবেস তৈরি করুন।" >&2
  exit 1
fi

exec python3.8 webui.py
