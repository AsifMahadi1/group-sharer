"""Userbot engine: shares text to target groups via Telethon (MTProto)."""
import asyncio
import random
import time
import json
import os

from telethon import TelegramClient, utils
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest, SetTypingRequest
from telethon.tl.types import ChatInviteAlready, SendMessageTypingAction
from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    ChatAdminRequiredError,
    UserBannedInChannelError,
    SlowModeWaitError,
)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
SESSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "userbot.session")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


class UserbotEngine:
    def __init__(self):
        self.config = load_config()
        self.client = None

    @property
    def is_logged_in(self):
        return self.client is not None and self.client.is_connected()

    async def ensure_connected(self):
        """Make sure the MTProto client exists, is connected and authorized.
        Returns (ok, error_or_None)."""
        try:
            if self.client is None:
                cfg = self.config
                self.client = TelegramClient(
                    SESSION_PATH,
                    cfg["api_id"],
                    cfg["api_hash"],
                    device_model="GroupSharer",
                    app_version="1.0",
                    system_version="Android 14",
                )
            if not self.client.is_connected():
                await self.client.connect()
            if not await self.client.is_user_authorized():
                return False, "not_authorized"
            return True, None
        except Exception as e:
            return False, str(e)

    async def start(self):
        cfg = self.config
        self.client = TelegramClient(
            SESSION_PATH,
            cfg["api_id"],
            cfg["api_hash"],
            device_model="GroupSharer",
            app_version="1.0",
            system_version="Android 14",
        )
        await self.client.connect()
        return self.client.is_user_authorized() if self.client.is_connected() else False

    async def stop(self):
        if self.client:
            await self.client.disconnect()

    async def request_login_code(self, phone):
        """Send OTP to phone. Returns (ok, error_or_None)."""
        cfg = self.config
        # Recreate client if not started
        if self.client is None or not self.client.is_connected():
            self.client = TelegramClient(
                SESSION_PATH, cfg["api_id"], cfg["api_hash"]
            )
            await self.client.connect()
        try:
            await self.client.send_code_request(phone)
            return True, None
        except Exception as e:
            return False, str(e)

    async def complete_login(self, phone, code, password=None):
        """Complete login with OTP (and 2FA password if needed)."""
        try:
            await self.client.sign_in(phone, code)
            return True, None
        except Exception as e:
            err = str(e)
            if "Password" in err or "2FA" in err or "SessionPasswordNeeded" in err:
                if password:
                    try:
                        await self.client.sign_in(password=password)
                        return True, None
                    except Exception as e2:
                        return False, str(e2)
                return False, "2FA_PASSWORD_REQUIRED"
            return False, err

    async def resolve_entity(self, target):
        """Resolve a chat link/username/id to an entity. Returns (entity, error).

        Supports:
          - public username / t.me link   -> get_entity
          - private invite link t.me/+XXX -> CheckChatInviteRequest (+ optional join)
          - numeric chat id               -> get_entity
        """
        target = str(target).strip()

        # --- Private invite link: t.me/+abc123 (or https://t.me/+abc123) ---
        if "+" in target.replace("t.me/", ""):
            try:
                hash_part = target.split("t.me/")[-1]
                if hash_part.startswith("+"):
                    hash_part = hash_part[1:]
                inv = await self.client(CheckChatInviteRequest(hash_part))
                if isinstance(inv, ChatInviteAlready):
                    # Already a member -> resolve the chat directly
                    chat = await self.client.get_entity(inv.chat)
                    return chat, None
                # Not a member -> try to join, then resolve
                try:
                    updates = await self.client(ImportChatInviteRequest(hash_part))
                    chat = updates.chats[0]
                    return chat, None
                except Exception as e:
                    return None, f"join failed: {e}"
            except Exception as e:
                return None, f"invite: {e}"

        # --- Numeric id (e.g. -1001234567890) ---
        if str(target).lstrip("-").isdigit():
            try:
                entity = await self.client.get_entity(int(target))
                return entity, None
            except Exception as e:
                return None, str(e)

        # --- Public username / t.me link ---
        try:
            entity = await self.client.get_entity(target)
            return entity, None
        except Exception as e:
            return None, str(e)

    async def list_joined_chats(self):
        """List all groups/channels/supergroups the logged-in account has joined.
        Returns list of dicts: {chat_id, title, type, username}"""
        from telethon.tl.types import Channel, Chat, User
        out = []
        try:
            async for d in self.client.iter_dialogs():
                e = d.entity
                if isinstance(e, User):
                    continue
                if isinstance(e, Channel):
                    kind = "channel" if e.broadcast else "supergroup"
                    if getattr(e, "megagroup", False):
                        kind = "supergroup"
                    out.append({
                        "chat_id": utils.get_peer_id(e),
                        "title": e.title or "",
                        "type": kind + (" 🔒" if getattr(e, "restricted", False) else ""),
                        "username": f"@{e.username}" if getattr(e, "username", None) else "—",
                    })
                elif isinstance(e, Chat):
                    out.append({
                        "chat_id": utils.get_peer_id(e),
                        "title": e.title or "",
                        "type": "group",
                        "username": "—",
                    })
        except Exception as e:
            return [], str(e)
        return out, None

    async def send_typing(self, entity):
        """Show 'typing...' in a group (makes posting look human). Best effort."""
        try:
            peer = await self.client.get_input_peer(entity)
            await self.client(SetTypingRequest(peer, SendMessageTypingAction()))
            return True
        except Exception:
            return False

    async def send_to_group(self, entity, text, delay_before=0, should_stop=None):
        """Send text to one group. Returns (status, reason)."""
        if delay_before > 0:
            # Sleep in small slices so /stop can interrupt quickly
            remaining = float(delay_before)
            while remaining > 0:
                if should_stop and should_stop():
                    return "stopped", "cancelled by owner"
                await asyncio.sleep(min(2.0, remaining))
                remaining -= 2.0
        if should_stop and should_stop():
            return "stopped", "cancelled by owner"
        try:
            await self.client.send_message(entity, text)
            return "sent", None
        except FloodWaitError as e:
            return "flood_wait", f"Flood wait {e.seconds}s"
        except SlowModeWaitError as e:
            return "slowmode", f"Slowmode wait {e.seconds}s"
        except ChatWriteForbiddenError:
            return "no_permission", "Bot cannot write in this chat"
        except ChatAdminRequiredError:
            return "no_permission", "Admin rights required"
        except UserBannedInChannelError:
            return "banned", "Account banned in this chat"
        except Exception as e:
            return "error", str(e)

    async def share_to_groups(self, groups, text, report_func=None, should_stop=None,
                              on_before_each=None, extra_delay_before=0):
        """Share text to all groups with random delays.
        groups: list of dicts {chat_id, title, entity}
        report_func: async callback (group, status, reason)
        should_stop: sync callable -> True to abort remaining sends
        on_before_each: async callback (group, index) called right before each send
        extra_delay_before: extra seconds to wait before the very first send
        Returns list of results.
        """
        cfg = self.config
        results = []
        stopped = False
        for i, g in enumerate(groups):
            if should_stop and should_stop():
                stopped = True
                break
            if on_before_each:
                await on_before_each(g, i)
            delay = random.randint(cfg.get("delay_min", 20), cfg.get("delay_max", 60))
            if i == 0:
                delay += extra_delay_before
            status, reason = await self.send_to_group(
                g["entity"], text, delay_before=delay, should_stop=should_stop
            )
            if status == "stopped":
                stopped = True
                break
            results.append({"title": g["title"], "status": status, "reason": reason})
            if report_func:
                await report_func(g, status, reason)
        return results, stopped