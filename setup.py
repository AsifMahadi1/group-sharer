#!/usr/bin/env python3
"""One-time setup: fill config.json interactively."""
import json
import os
import sys

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def ask(prompt, default=None, secret=False):
    if default is not None:
        prompt = f"{prompt} [{default}]: "
    else:
        prompt = f"{prompt}: "
    val = input(prompt).strip()
    if not val and default is not None:
        return default
    return val


def main():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)

    print("=== Group Sharer Setup ===\n")
    print("1) Bot Token: @BotFather থেকে নিন (/newbot) — https://t.me/BotFather")
    cfg["bot_token"] = ask("Bot Token", cfg.get("bot_token", ""))

    print("\n2) API credentials: https://my.telegram.org → API development tools")
    cfg["api_id"] = int(ask("API ID", cfg.get("api_id", 0)))
    cfg["api_hash"] = ask("API Hash", cfg.get("api_hash", ""))

    print("\n3) Owner Telegram ID: @userinfobot থেকে নিন")
    cfg["owner_id"] = int(ask("Owner ID", cfg.get("owner_id", 0)))

    print("\n4) Delay settings (seconds between groups)")
    cfg["delay_min"] = int(ask("Min delay", cfg.get("delay_min", 20)))
    cfg["delay_max"] = int(ask("Max delay", cfg.get("delay_max", 60)))
    cfg["max_flood_wait"] = int(ask("Max flood wait", cfg.get("max_flood_wait", 300)))

    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    print("\n✅ config.json সেভ হয়েছে!")

    missing = []
    if not cfg["bot_token"]:
        missing.append("bot_token")
    if not cfg["api_id"] or not cfg["api_hash"]:
        missing.append("api_id/api_hash")
    if not cfg["owner_id"]:
        missing.append("owner_id")
    if missing:
        print(f"⚠️ এখনো বাকি: {', '.join(missing)} — এগুলো ছাড়া বট চলবে না।")
        sys.exit(1)
    print("🚀 এখন চালান: python3 bot.py")


if __name__ == "__main__":
    main()