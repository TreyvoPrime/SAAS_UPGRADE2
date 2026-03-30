from __future__ import annotations

import asyncio
import copy
import re
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import discord

from core.storage import read_json, write_json


DEFENSE_FEATURES = ("linkblock", "inviteblock", "antispam", "antijoin", "mentionguard", "lockdown")

URL_PATTERN = re.compile(r"(https?://\S+|www\.\S+|discord(?:\.gg|(?:app)?\.com/invite)/\S+)", re.IGNORECASE)
INVITE_PATTERN = re.compile(r"(discord(?:\.gg|(?:app)?\.com/invite)/\S+)", re.IGNORECASE)

DEFAULT_GUILD_DEFENSES = {
    "linkblock": {
        "enabled": False,
        "ends_at": None,
    },
    "inviteblock": {
        "enabled": False,
        "ends_at": None,
    },
    "antispam": {
        "enabled": False,
        "ends_at": None,
        "message_limit": 5,
        "window_seconds": 8,
        "timeout_seconds": 60,
    },
    "antijoin": {
        "enabled": False,
        "ends_at": None,
        "action": "kick",
    },
    "mentionguard": {
        "enabled": False,
        "ends_at": None,
        "mention_limit": 5,
        "timeout_seconds": 60,
    },
    "lockdown": {
        "enabled": False,
        "ends_at": None,
        "allowed_role_ids": [],
        "snapshot": {},
    },
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def _normalize_role_ids(role_ids: list[int] | list[str] | None) -> list[int]:
    normalized: list[int] = []
    for role_id in role_ids or []:
        try:
            normalized.append(int(role_id))
        except (TypeError, ValueError):
            continue
    return sorted(dict.fromkeys(normalized))


def _is_discord_invite_text(text: str) -> bool:
    lowered = text.lower()
    return "discord.gg/" in lowered or "discord.com/invite/" in lowered or "discordapp.com/invite/" in lowered


class ServerDefenseStore:
    def __init__(self, path: str | Path = "dashboard_data/server_defense.json"):
        self.path = Path(path)
        self.data = read_json(self.path, {"guilds": {}})

    def save(self) -> None:
        write_json(self.path, self.data)

    def _guild_bucket(self, guild_id: int) -> dict[str, Any]:
        guilds = self.data.setdefault("guilds", {})
        guild_bucket = guilds.setdefault(str(guild_id), {})
        for feature_name, defaults in DEFAULT_GUILD_DEFENSES.items():
            bucket = guild_bucket.setdefault(feature_name, copy.deepcopy(defaults))
            for key, value in defaults.items():
                bucket.setdefault(key, copy.deepcopy(value))
        return guild_bucket

    def all_guild_ids(self) -> list[int]:
        guilds = self.data.get("guilds", {})
        return [int(guild_id) for guild_id in guilds if str(guild_id).isdigit()]

    def get_feature(self, guild_id: int, feature: str) -> dict[str, Any]:
        bucket = copy.deepcopy(self._guild_bucket(guild_id).get(feature, {}))
        if feature == "lockdown":
            bucket["allowed_role_ids"] = _normalize_role_ids(bucket.get("allowed_role_ids", []))
            bucket["snapshot"] = bucket.get("snapshot", {}) or {}
        return bucket

    def get_all(self, guild_id: int) -> dict[str, dict[str, Any]]:
        self._guild_bucket(guild_id)
        return {feature: self.get_feature(guild_id, feature) for feature in DEFENSE_FEATURES}

    def patch_feature(self, guild_id: int, feature: str, **changes) -> dict[str, Any]:
        bucket = self._guild_bucket(guild_id).setdefault(feature, copy.deepcopy(DEFAULT_GUILD_DEFENSES[feature]))
        bucket.update(changes)
        self.save()
        return self.get_feature(guild_id, feature)

    def set_lockdown_snapshot(self, guild_id: int, snapshot: dict[str, Any]) -> dict[str, Any]:
        return self.patch_feature(guild_id, "lockdown", snapshot=snapshot)

    def clear_lockdown_snapshot(self, guild_id: int) -> dict[str, Any]:
        return self.patch_feature(guild_id, "lockdown", snapshot={})


class ServerDefenseManager:
    def __init__(self, bot: discord.Client, store: ServerDefenseStore | None = None):
        self.bot = bot
        self.store = store or ServerDefenseStore()
        self._expiry_tasks: dict[tuple[int, str], asyncio.Task] = {}
        self._spam_windows: dict[tuple[int, int], deque[datetime]] = defaultdict(deque)
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self.initialize()

    async def stop(self) -> None:
        self._started = False
        for task in list(self._expiry_tasks.values()):
            task.cancel()
        for task in list(self._expiry_tasks.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._expiry_tasks.clear()
        self._spam_windows.clear()

    async def initialize(self) -> None:
        for guild_id in self.store.all_guild_ids():
            for feature in DEFENSE_FEATURES:
                state = self.store.get_feature(guild_id, feature)
                if not state.get("enabled"):
                    continue
                ends_at = from_iso(state.get("ends_at"))
                if ends_at and ends_at <= utcnow():
                    if feature == "lockdown":
                        await self.disable_feature(
                            guild_id,
                            feature,
                            reason="Lockdown timer expired while the bot was restarting.",
                        )
                    else:
                        self.store.patch_feature(guild_id, feature, enabled=False, ends_at=None)
                elif ends_at:
                    self._schedule_expiry(guild_id, feature, ends_at)

    def get_dashboard_state(self, guild_id: int) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        active_count = 0

        for feature in DEFENSE_FEATURES:
            state = self.store.get_feature(guild_id, feature)
            ends_at = from_iso(state.get("ends_at"))
            if state.get("enabled") and ends_at and ends_at <= utcnow() and feature != "lockdown":
                state = self.store.patch_feature(guild_id, feature, enabled=False, ends_at=None)
            if state.get("enabled"):
                active_count += 1
            payload[feature] = state

        payload["active_count"] = active_count
        return payload

    def build_dashboard_state(self, guild_id: int, role_lookup: dict[int, str] | None = None) -> dict[str, Any]:
        role_lookup = role_lookup or {}
        state = self.get_dashboard_state(guild_id)

        def remaining_minutes(item: dict[str, Any]) -> int | None:
            ends_at = from_iso(item.get("ends_at"))
            if ends_at is None:
                return None
            delta = max(int((ends_at - utcnow()).total_seconds() // 60), 0)
            return delta or 1

        def make_card(
            feature: str,
            title: str,
            tag: str,
            description: str,
            *,
            rate_label: str | None = None,
        ) -> dict[str, Any]:
            item = state[feature]
            lockdown_roles = item.get("allowed_role_ids", []) if feature == "lockdown" else []
            minutes_left = remaining_minutes(item)
            allowed_role_names = [role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in lockdown_roles]
            return {
                "name": feature,
                "title": title,
                "tag": tag,
                "description": description,
                "enabled": item.get("enabled", False),
                "duration_minutes": minutes_left,
                "duration_label": "Until disabled" if not item.get("ends_at") else f"{minutes_left} minute window",
                "status_label": "Armed" if item.get("enabled") else "Offline",
                "remaining_label": item.get("ends_at") or "No timer",
                "tone": "danger" if item.get("enabled") else "muted",
                "rate_label": rate_label,
                "allowed_role_ids": lockdown_roles,
                "allowed_role_names": allowed_role_names,
                "allowed_role_summary": ", ".join(allowed_role_names) if allowed_role_names else "Only server staff can talk",
            }

        cards = [
            make_card(
                "linkblock",
                "Link Block",
                "Inbound links",
                "Blocks external URLs before they land in chat.",
            ),
            make_card(
                "inviteblock",
                "Invite Block",
                "Discord invites",
                "Blocks Discord invite links separately from normal URLs.",
            ),
            make_card(
                "antispam",
                "Anti Spam",
                "Message rate",
                "Limits each user to five messages in a short burst until the shield is turned off.",
                rate_label="5 messages / 8 seconds",
            ),
            make_card(
                "antijoin",
                "Anti Join",
                "Join control",
                "Kicks new joins while the shield is active so raids cannot build momentum.",
            ),
            make_card(
                "mentionguard",
                "Mention Guard",
                "Ping shield",
                "Blocks mention bursts before someone can light the whole server up.",
                rate_label="5 mentions / message",
            ),
            make_card(
                "lockdown",
                "Lockdown",
                "Channel freeze",
                "Locks the server down and keeps selected talk roles moving while the server is under pressure.",
            ),
        ]

        timed_count = len([card for card in cards if card["enabled"] and state[card["name"]].get("ends_at")])
        lockdown_roles = state["lockdown"].get("allowed_role_ids", [])
        return {
            "cards": cards,
            "active_count": state["active_count"],
            "timed_count": timed_count,
            "lockdown_role_ids": lockdown_roles,
            "lockdown_role_names": [role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in lockdown_roles],
            "lockdown_role_count": len(lockdown_roles),
        }

    def _cancel_expiry(self, guild_id: int, feature: str) -> None:
        task = self._expiry_tasks.pop((guild_id, feature), None)
        if task and not task.done():
            task.cancel()

    def _schedule_expiry(self, guild_id: int, feature: str, ends_at: datetime | None) -> None:
        self._cancel_expiry(guild_id, feature)
        if ends_at is None:
            return

        async def runner() -> None:
            delay = max((ends_at - utcnow()).total_seconds(), 0)
            try:
                await asyncio.sleep(delay)
                if feature == "lockdown":
                    await self.disable_feature(guild_id, feature, reason="Lockdown timer expired.")
                else:
                    self.store.patch_feature(guild_id, feature, enabled=False, ends_at=None)
            except asyncio.CancelledError:
                return
            finally:
                self._expiry_tasks.pop((guild_id, feature), None)

        self._expiry_tasks[(guild_id, feature)] = asyncio.create_task(
            runner(),
            name=f"server-defense-{guild_id}-{feature}",
        )

    def _member_is_exempt(self, member: discord.Member) -> bool:
        return member.guild_permissions.administrator or member.guild_permissions.manage_guild

    async def _warn_channel(self, channel: discord.abc.Messageable, message: str) -> None:
        try:
            warning = await channel.send(message)
            await warning.delete(delay=6)
        except Exception:
            return

    async def _timeout_member(self, member: discord.Member, seconds: int, reason: str) -> None:
        if seconds <= 0:
            return
        me = member.guild.me
        if me is None or not me.guild_permissions.moderate_members:
            return
        try:
            await member.timeout(utcnow() + timedelta(seconds=seconds), reason=reason)
        except Exception:
            return

    async def _delete_message(self, message: discord.Message, warning: str) -> None:
        try:
            await message.delete()
        except Exception:
            return
        await self._warn_channel(message.channel, warning)

    def _is_active(self, state: dict[str, Any]) -> bool:
        if not state.get("enabled"):
            return False
        ends_at = from_iso(state.get("ends_at"))
        return not (ends_at and ends_at <= utcnow())

    def _contains_link(self, text: str) -> bool:
        return bool(URL_PATTERN.search(text or ""))

    def _contains_invite(self, text: str) -> bool:
        return bool(INVITE_PATTERN.search(text or "")) or _is_discord_invite_text(text or "")

    async def process_message(self, message: discord.Message) -> bool:
        if message.guild is None or not isinstance(message.author, discord.Member):
            return False
        if message.author.bot or self._member_is_exempt(message.author):
            return False

        guild_id = message.guild.id
        content = message.content or ""

        linkblock = self.store.get_feature(guild_id, "linkblock")
        if self._is_active(linkblock) and self._contains_link(content) and not self._contains_invite(content):
            await self._delete_message(message, "Link Block prevented that message.")
            return True

        inviteblock = self.store.get_feature(guild_id, "inviteblock")
        if self._is_active(inviteblock) and self._contains_invite(content):
            await self._delete_message(message, "Invite Block prevented that message.")
            return True

        mentionguard = self.store.get_feature(guild_id, "mentionguard")
        mention_limit = int(mentionguard.get("mention_limit", 5))
        if self._is_active(mentionguard) and len(message.mentions) >= mention_limit:
            await self._delete_message(message, f"Mention Guard blocked that message ({mention_limit}+ mentions).")
            await self._timeout_member(
                message.author,
                int(mentionguard.get("timeout_seconds", 60)),
                "Mention Guard triggered.",
            )
            return True

        antispam = self.store.get_feature(guild_id, "antispam")
        if self._is_active(antispam):
            bucket = self._spam_windows[(guild_id, message.author.id)]
            now = utcnow()
            bucket.append(now)
            window_seconds = int(antispam.get("window_seconds", 8))
            message_limit = int(antispam.get("message_limit", 5))
            while bucket and (now - bucket[0]).total_seconds() > window_seconds:
                bucket.popleft()
            if len(bucket) > message_limit:
                await self._delete_message(
                    message,
                    f"Anti-spam active: more than {message_limit} messages in {window_seconds} seconds.",
                )
                await self._timeout_member(
                    message.author,
                    int(antispam.get("timeout_seconds", 60)),
                    "Anti-spam triggered.",
                )
                bucket.clear()
                return True

        return False

    async def handle_member_join(self, member: discord.Member) -> bool:
        antijoin = self.store.get_feature(member.guild.id, "antijoin")
        if not self._is_active(antijoin):
            return False

        me = member.guild.me
        if me is None or not me.guild_permissions.kick_members:
            return False

        try:
            await member.kick(reason="Anti-join is active.")
            return True
        except Exception:
            return False

    async def enable_feature(
        self,
        guild_id: int,
        feature: str,
        *,
        duration_minutes: int | None = None,
        actor: discord.abc.User | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        ends_at = utcnow() + timedelta(minutes=duration_minutes) if duration_minutes else None

        if feature == "lockdown":
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                raise ValueError("Guild unavailable for lockdown.")
            state = await self._apply_lockdown(guild, ends_at=ends_at, actor=actor, reason=reason)
        else:
            state = self.store.patch_feature(guild_id, feature, enabled=True, ends_at=to_iso(ends_at))

        self._schedule_expiry(guild_id, feature, ends_at)
        return state

    async def disable_feature(
        self,
        guild_id: int,
        feature: str,
        *,
        reason: str | None = None,
        actor: discord.abc.User | None = None,
    ) -> dict[str, Any]:
        self._cancel_expiry(guild_id, feature)
        if feature == "lockdown":
            return await self._lift_lockdown(guild_id, reason=reason, actor=actor)
        return self.store.patch_feature(guild_id, feature, enabled=False, ends_at=None)

    async def set_duration(
        self,
        guild_id: int,
        feature: str,
        duration_minutes: int | None,
    ) -> dict[str, Any]:
        state = self.store.get_feature(guild_id, feature)
        if not state.get("enabled"):
            return state
        ends_at = utcnow() + timedelta(minutes=duration_minutes) if duration_minutes else None
        state = self.store.patch_feature(guild_id, feature, ends_at=to_iso(ends_at))
        self._schedule_expiry(guild_id, feature, ends_at)
        return state

    async def set_lockdown_roles(self, guild_id: int, role_ids: list[int]) -> dict[str, Any]:
        cleaned_role_ids = _normalize_role_ids(role_ids)
        state = self.store.get_feature(guild_id, "lockdown")
        updated = self.store.patch_feature(guild_id, "lockdown", allowed_role_ids=cleaned_role_ids)
        if state.get("enabled"):
            asyncio.create_task(self._refresh_lockdown_roles(guild_id))
        return updated

    async def set_defense(
        self,
        guild_id: int,
        feature: str,
        *,
        enabled: bool,
        duration_minutes: int | None = None,
    ) -> dict[str, Any]:
        normalized = "mentionguard" if feature == "mentionblock" else feature
        if normalized not in DEFENSE_FEATURES:
            raise ValueError(f"Unknown defense feature: {feature}")
        current = self.store.get_feature(guild_id, normalized)
        if enabled:
            if current.get("enabled"):
                return await self.set_duration(guild_id, normalized, duration_minutes)
            return await self.enable_feature(guild_id, normalized, duration_minutes=duration_minutes)
        return await self.disable_feature(guild_id, normalized)

    async def _refresh_lockdown_roles(self, guild_id: int) -> None:
        state = self.store.get_feature(guild_id, "lockdown")
        snapshot = state.get("snapshot", {})
        guild = self.bot.get_guild(guild_id)
        if guild is None or not snapshot:
            return

        allowed_role_ids = set(state.get("allowed_role_ids", []))
        updated_snapshot = copy.deepcopy(snapshot)
        for channel_id, channel_snapshot in snapshot.items():
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel):
                continue

            await self._set_channel_send_permission(
                channel,
                guild.default_role,
                False,
                reason="Refreshing lockdown role permissions.",
            )

            tracked_role_states = channel_snapshot.setdefault("roles", {})
            for role_id_str, prior_value in list(tracked_role_states.items()):
                role_id = int(role_id_str)
                role = guild.get_role(role_id)
                if role is None:
                    continue
                if role_id not in allowed_role_ids:
                    await self._set_channel_send_permission(
                        channel,
                        role,
                        prior_value,
                        reason="Restoring role permission removed from lockdown allow-list.",
                    )
                    tracked_role_states.pop(role_id_str, None)

            for role_id in allowed_role_ids:
                role = guild.get_role(role_id)
                if role is None:
                    continue
                key = str(role_id)
                if key not in tracked_role_states:
                    tracked_role_states[key] = channel.overwrites_for(role).send_messages
                await self._set_channel_send_permission(
                    channel,
                    role,
                    True,
                    reason="Applying lockdown speaker role.",
                )

        self.store.set_lockdown_snapshot(guild_id, updated_snapshot)

    async def _apply_lockdown(
        self,
        guild: discord.Guild,
        *,
        ends_at: datetime | None,
        actor: discord.abc.User | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        state = self.store.get_feature(guild.id, "lockdown")
        allowed_role_ids = set(state.get("allowed_role_ids", []))
        snapshot: dict[str, Any] = {}

        for channel in guild.text_channels:
            channel_snapshot = {
                "default_send_messages": channel.overwrites_for(guild.default_role).send_messages,
                "roles": {},
            }
            await self._set_channel_send_permission(
                channel,
                guild.default_role,
                False,
                reason=reason or f"Server lockdown enabled by {actor or 'ServerDefense'}",
            )
            for role_id in allowed_role_ids:
                role = guild.get_role(role_id)
                if role is None:
                    continue
                overwrite = channel.overwrites_for(role)
                channel_snapshot["roles"][str(role.id)] = overwrite.send_messages
                await self._set_channel_send_permission(
                    channel,
                    role,
                    True,
                    reason=reason or "Server lockdown speaker role update.",
                )
            snapshot[str(channel.id)] = channel_snapshot

        return self.store.patch_feature(
            guild.id,
            "lockdown",
            enabled=True,
            ends_at=to_iso(ends_at),
            snapshot=snapshot,
        )

    async def _lift_lockdown(
        self,
        guild_id: int,
        *,
        reason: str | None = None,
        actor: discord.abc.User | None = None,
    ) -> dict[str, Any]:
        state = self.store.get_feature(guild_id, "lockdown")
        guild = self.bot.get_guild(guild_id)
        snapshot = state.get("snapshot", {})

        if guild is not None:
            for channel_id, channel_snapshot in snapshot.items():
                channel = guild.get_channel(int(channel_id))
                if not isinstance(channel, discord.TextChannel):
                    continue
                await self._set_channel_send_permission(
                    channel,
                    guild.default_role,
                    channel_snapshot.get("default_send_messages"),
                    reason=reason or f"Server lockdown disabled by {actor or 'ServerDefense'}",
                )
                for role_id_str, send_state in channel_snapshot.get("roles", {}).items():
                    role = guild.get_role(int(role_id_str))
                    if role is None:
                        continue
                    await self._set_channel_send_permission(
                        channel,
                        role,
                        send_state,
                        reason=reason or "Restoring lockdown role overwrite.",
                    )

        return self.store.patch_feature(
            guild_id,
            "lockdown",
            enabled=False,
            ends_at=None,
            snapshot={},
        )

    async def _set_channel_send_permission(
        self,
        channel: discord.TextChannel,
        target: discord.Role,
        send_messages: bool | None,
        *,
        reason: str | None = None,
    ) -> None:
        overwrite = channel.overwrites_for(target)
        overwrite.send_messages = send_messages
        try:
            await channel.set_permissions(target, overwrite=overwrite, reason=reason)
        except Exception:
            return
