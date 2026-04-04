from __future__ import annotations

from pathlib import Path
import threading
from typing import Any

import discord

from core.storage import read_json, write_json


DEFAULT_WELCOME_MESSAGE = "Hello {user}, welcome to {server}."
DEFAULT_LEAVE_MESSAGE = "{user_name} left {server}."
DEFAULT_JOIN_DM_MESSAGE = "Welcome to {server}, {display_name}. Read the server guide and check the rules channel to get started."
GREETING_DATA_PATH = Path("greetings.json")
LEGACY_WELCOME_PATH = Path("welcome_channels.json")


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class GreetingsStore:
    def __init__(self, path: Path | None = None):
        self.path = path or GREETING_DATA_PATH
        self._default: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._data = read_json(self.path, self._default)
        self._migrate_legacy_welcome_channels()

    def _save(self) -> None:
        write_json(self.path, self._data)

    def _refresh(self) -> None:
        self._data = read_json(self.path, self._default)

    def _migrate_legacy_welcome_channels(self) -> None:
        with self._lock:
            if self._data or not LEGACY_WELCOME_PATH.exists():
                return

            legacy_data = read_json(LEGACY_WELCOME_PATH, {})
            if not isinstance(legacy_data, dict):
                return

            migrated: dict[str, dict[str, Any]] = {}
            for guild_id, channel_id in legacy_data.items():
                try:
                    normalized_channel_id = int(channel_id)
                except (TypeError, ValueError):
                    continue

                migrated[str(guild_id)] = {
                    "welcome_channel_id": normalized_channel_id,
                    "welcome_message": DEFAULT_WELCOME_MESSAGE,
                    "leave_channel_id": None,
                    "leave_message": DEFAULT_LEAVE_MESSAGE,
                    "join_dm_enabled": False,
                    "join_dm_message": DEFAULT_JOIN_DM_MESSAGE,
                }

            if migrated:
                self._data = migrated
                self._save()

    def _guild_key(self, guild_id: int) -> str:
        return str(guild_id)

    def get_guild(self, guild_id: int) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            guild_key = self._guild_key(guild_id)
            state = self._data.get(guild_key, {})
            return {
                "welcome_channel_id": self._coerce_int(state.get("welcome_channel_id")),
                "welcome_message": str(state.get("welcome_message") or DEFAULT_WELCOME_MESSAGE),
                "leave_channel_id": self._coerce_int(state.get("leave_channel_id")),
                "leave_message": str(state.get("leave_message") or DEFAULT_LEAVE_MESSAGE),
                "join_dm_enabled": bool(state.get("join_dm_enabled", False)),
                "join_dm_message": str(state.get("join_dm_message") or DEFAULT_JOIN_DM_MESSAGE),
            }

    def update_guild(
        self,
        guild_id: int,
        *,
        welcome_channel_id: int | None | object = ...,
        welcome_message: str | None | object = ...,
        leave_channel_id: int | None | object = ...,
        leave_message: str | None | object = ...,
        join_dm_enabled: bool | object = ...,
        join_dm_message: str | None | object = ...,
    ) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            current = self.get_guild(guild_id)

            if welcome_channel_id is not ...:
                current["welcome_channel_id"] = self._coerce_int(welcome_channel_id)
            if welcome_message is not ...:
                current["welcome_message"] = self._normalize_message(welcome_message, DEFAULT_WELCOME_MESSAGE)
            if leave_channel_id is not ...:
                current["leave_channel_id"] = self._coerce_int(leave_channel_id)
            if leave_message is not ...:
                current["leave_message"] = self._normalize_message(leave_message, DEFAULT_LEAVE_MESSAGE)
            if join_dm_enabled is not ...:
                current["join_dm_enabled"] = bool(join_dm_enabled)
            if join_dm_message is not ...:
                current["join_dm_message"] = self._normalize_message(join_dm_message, DEFAULT_JOIN_DM_MESSAGE)

            self._data[self._guild_key(guild_id)] = current
            self._save()
            return current

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value in (None, "", False):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_message(value: Any, fallback: str) -> str:
        text = str(value or "").strip()
        return text or fallback


class GreetingsManager:
    def __init__(self, bot: discord.Client, store: GreetingsStore):
        self.bot = bot
        self.store = store

    def get_dashboard_state(self, guild_id: int, channels: dict[int, str] | None = None) -> dict[str, Any]:
        state = self.store.get_guild(guild_id)
        channel_lookup = channels or {}
        return {
            "welcome": {
                "channel_id": state["welcome_channel_id"],
                "channel_name": channel_lookup.get(state["welcome_channel_id"], "Not configured")
                if state["welcome_channel_id"]
                else "Not configured",
                "message": state["welcome_message"],
                "enabled": bool(state["welcome_channel_id"]),
            },
            "leave": {
                "channel_id": state["leave_channel_id"],
                "channel_name": channel_lookup.get(state["leave_channel_id"], "Not configured")
                if state["leave_channel_id"]
                else "Not configured",
                "message": state["leave_message"],
                "enabled": bool(state["leave_channel_id"]),
            },
            "join_dm": {
                "enabled": bool(state["join_dm_enabled"]),
                "message": state["join_dm_message"],
            },
            "placeholders": [
                {"token": "{user}", "label": "Mentions the member"},
                {"token": "{user_name}", "label": "Uses the member name"},
                {"token": "{display_name}", "label": "Uses the server nickname"},
                {"token": "{server}", "label": "Uses the server name"},
                {"token": "{membercount}", "label": "Uses the current member count"},
            ],
        }

    def set_welcome(self, guild_id: int, *, channel_id: int | None | object = ..., message: str | None | object = ...) -> dict[str, Any]:
        return self.store.update_guild(
            guild_id,
            welcome_channel_id=channel_id,
            welcome_message=message,
        )

    def set_leave(self, guild_id: int, *, channel_id: int | None | object = ..., message: str | None | object = ...) -> dict[str, Any]:
        return self.store.update_guild(
            guild_id,
            leave_channel_id=channel_id,
            leave_message=message,
        )

    def set_join_dm(self, guild_id: int, *, enabled: bool | object = ..., message: str | None | object = ...) -> dict[str, Any]:
        return self.store.update_guild(
            guild_id,
            join_dm_enabled=enabled,
            join_dm_message=message,
        )

    async def send_welcome(self, member: discord.Member) -> bool:
        state = self.store.get_guild(member.guild.id)
        channel = self._resolve_channel(member.guild, state["welcome_channel_id"])
        sent = False
        if channel is not None:
            content = self.format_message(state["welcome_message"], member)
            try:
                await channel.send(content)
                sent = True
            except (discord.Forbidden, discord.HTTPException):
                sent = False
        await self.send_join_dm(member)
        return sent

    async def send_leave(self, member: discord.Member) -> bool:
        state = self.store.get_guild(member.guild.id)
        channel = self._resolve_channel(member.guild, state["leave_channel_id"])
        if channel is None:
            return False

        content = self.format_message(state["leave_message"], member)
        try:
            await channel.send(content)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def send_join_dm(self, member: discord.Member) -> bool:
        state = self.store.get_guild(member.guild.id)
        if not state.get("join_dm_enabled"):
            return False
        content = self.format_message(state["join_dm_message"], member)
        try:
            await member.send(content)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    def format_message(self, template: str, member: discord.Member) -> str:
        values = _SafeFormatDict(
            {
                "user": member.mention,
                "user_name": member.name,
                "username": member.name,
                "display_name": member.display_name,
                "server": member.guild.name,
                "membercount": member.guild.member_count or len(member.guild.members),
            }
        )
        return str(template or DEFAULT_WELCOME_MESSAGE).format_map(values)

    @staticmethod
    def _resolve_channel(guild: discord.Guild, channel_id: int | None) -> discord.TextChannel | None:
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        return channel if isinstance(channel, discord.TextChannel) else None
