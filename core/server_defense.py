from __future__ import annotations

import asyncio
import copy
import re
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import discord

from core.storage import read_json, write_json


DEFENSE_FEATURES = ("linkblock", "inviteblock", "antispam", "antijoin", "mentionguard", "autofilter", "lockdown", "antiraid")

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
        "window_seconds": 6,
        "timeout_seconds": 90,
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
        "window_seconds": 10,
        "timeout_seconds": 90,
    },
    "autofilter": {
        "enabled": False,
        "ends_at": None,
        "filter_terms": [],
        "warning_limit": 3,
        "timeout_minutes": 60,
        "warning_counts": {},
    },
    "lockdown": {
        "enabled": False,
        "ends_at": None,
        "allowed_role_ids": [],
        "snapshot": {},
    },
    "antiraid": {
        "enabled": False,
        "ends_at": None,
        "blocked_phrases": [],
        "allowed_domains": [],
        "preset": "balanced",
    },
}

LOCKDOWN_PERMISSION_KEYS = (
    "send_messages",
    "send_messages_in_threads",
    "add_reactions",
    "create_public_threads",
    "create_private_threads",
)

THREAT_LEVELS = (
    ("normal", "Normal", 0),
    ("moderate", "Moderate threat", 25),
    ("major", "Major threat", 50),
    ("high", "High threat", 75),
)
MAX_THREAT_SCORE = 100
THREAT_DECAY_PER_MINUTE = 4
RECENT_JOIN_MINUTES = 60
RECENT_REJOIN_MINUTES = 15


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


def _extract_domains(text: str) -> list[str]:
    matches = URL_PATTERN.findall(text or "")
    domains: list[str] = []
    for match in matches:
        value = match.lower()
        value = value.replace("https://", "").replace("http://", "").replace("www.", "")
        domain = value.split("/")[0].split("?")[0].strip(".")
        if domain:
            domains.append(domain)
    return domains


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
        if feature == "antiraid":
            bucket["blocked_phrases"] = [str(item).strip() for item in bucket.get("blocked_phrases", []) if str(item).strip()][:50]
            bucket["allowed_domains"] = [str(item).strip().lower() for item in bucket.get("allowed_domains", []) if str(item).strip()][:50]
            bucket["preset"] = str(bucket.get("preset") or "balanced")
        if feature == "autofilter":
            bucket["filter_terms"] = [str(item).strip().lower() for item in bucket.get("filter_terms", []) if str(item).strip()][:100]
            bucket["warning_limit"] = max(1, int(bucket.get("warning_limit", 3)))
            bucket["timeout_minutes"] = max(1, int(bucket.get("timeout_minutes", 60)))
            bucket["warning_counts"] = {
                str(user_id): int(count)
                for user_id, count in (bucket.get("warning_counts") or {}).items()
                if str(user_id).isdigit()
            }
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
        self._spam_windows: dict[tuple[int, int], deque[tuple[datetime, discord.Message]]] = defaultdict(deque)
        self._mention_windows: dict[tuple[int, int], deque[tuple[datetime, discord.Message, int]]] = defaultdict(deque)
        self._guild_message_windows: dict[int, deque[dict[str, Any]]] = defaultdict(deque)
        self._join_windows: dict[int, deque[dict[str, Any]]] = defaultdict(deque)
        self._leave_windows: dict[int, deque[dict[str, Any]]] = defaultdict(deque)
        self._reaction_windows: dict[int, deque[dict[str, Any]]] = defaultdict(deque)
        self._module_trigger_windows: dict[int, deque[tuple[datetime, str]]] = defaultdict(deque)
        self._member_leave_times: dict[tuple[int, int], datetime] = {}
        self._threat_state: dict[int, dict[str, Any]] = defaultdict(self._new_threat_state)
        self._threat_cooldowns: dict[tuple[int, str], datetime] = {}
        self._started = False

    def _guardian_available(self, guild_id: int) -> bool:
        controls = getattr(self.bot, "command_controls", None)
        if controls is None or not hasattr(controls, "is_premium_enabled"):
            return False
        return bool(controls.is_premium_enabled(guild_id))

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
        self._mention_windows.clear()
        self._guild_message_windows.clear()
        self._join_windows.clear()
        self._leave_windows.clear()
        self._reaction_windows.clear()
        self._module_trigger_windows.clear()
        self._member_leave_times.clear()
        self._threat_state.clear()
        self._threat_cooldowns.clear()

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

    def _new_threat_state(self) -> dict[str, Any]:
        return {
            "score": 0.0,
            "last_decay_at": utcnow(),
            "recent_signals": deque(maxlen=8),
            "recent_actions": deque(maxlen=5),
            "current_level": "normal",
            "last_notified_level": "normal",
            "last_auto_level": "normal",
            "raid_mode_active": False,
        }

    def _trim_payload_window(self, bucket: deque[dict[str, Any]], now: datetime, window_seconds: int) -> None:
        while bucket and (now - bucket[0]["time"]).total_seconds() > window_seconds:
            bucket.popleft()

    def _trim_simple_window(self, bucket: deque[tuple[datetime, str]], now: datetime, window_seconds: int) -> None:
        while bucket and (now - bucket[0][0]).total_seconds() > window_seconds:
            bucket.popleft()

    def _normalize_text_signature(self, value: str) -> str:
        if not value:
            return ""
        normalized = URL_PATTERN.sub("<url>", value.lower())
        normalized = re.sub(r"discord(?:\.gg|(?:app)?\.com/invite)/\S+", "<invite>", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = re.sub(r"[^a-z0-9<>\s]", "", normalized)
        return normalized.strip()[:140]

    def _name_signature(self, value: str) -> str:
        cleaned = re.sub(r"[^a-z]", "", (value or "").lower())
        if len(cleaned) < 5:
            return ""
        return cleaned[:7]

    def _member_has_default_profile(self, member: discord.Member) -> bool:
        return member.avatar is None

    def _member_is_recent_join(self, member: discord.Member, *, within_minutes: int = RECENT_JOIN_MINUTES) -> bool:
        if member.joined_at is None:
            return False
        return (utcnow() - member.joined_at.astimezone(UTC)) <= timedelta(minutes=within_minutes)

    def _member_account_age_days(self, member: discord.Member) -> float:
        return max((utcnow() - member.created_at.astimezone(UTC)).total_seconds() / 86400, 0.0)

    def _staff_role_pinged(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False
        for role in message.role_mentions:
            permissions = role.permissions
            if (
                permissions.administrator
                or permissions.manage_guild
                or permissions.manage_messages
                or permissions.moderate_members
                or permissions.kick_members
                or permissions.ban_members
            ):
                return True
        return False

    def _score_level(self, score: float) -> tuple[str, str]:
        if score >= 75:
            return ("high", "High threat")
        if score >= 50:
            return ("major", "Major threat")
        if score >= 25:
            return ("moderate", "Moderate threat")
        return ("normal", "Normal")

    def _level_rank(self, level_key: str) -> int:
        order = {"normal": 0, "moderate": 1, "major": 2, "high": 3}
        return order.get(level_key, 0)

    def _apply_threat_decay(self, guild_id: int, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        state = self._threat_state[guild_id]
        last_decay_at = state.get("last_decay_at", now)
        elapsed_minutes = max((now - last_decay_at).total_seconds() / 60, 0)
        if elapsed_minutes > 0:
            state["score"] = max(state.get("score", 0.0) - (elapsed_minutes * THREAT_DECAY_PER_MINUTE), 0.0)
            state["last_decay_at"] = now

        level_key, _ = self._score_level(state["score"])
        state["current_level"] = level_key
        state["raid_mode_active"] = level_key == "high"
        if level_key == "normal" and state["score"] < 8:
            state["last_notified_level"] = "normal"
            state["last_auto_level"] = "normal"
        return state

    def _signal_ready(self, guild_id: int, signal_key: str, cooldown_seconds: int, now: datetime) -> bool:
        cooldown_key = (guild_id, signal_key)
        active_until = self._threat_cooldowns.get(cooldown_key)
        if active_until and active_until > now:
            return False
        self._threat_cooldowns[cooldown_key] = now + timedelta(seconds=cooldown_seconds)
        return True

    async def _emit_threat_event(
        self,
        guild_id: int,
        *,
        title: str,
        description: str,
        fields: list[tuple[str, str, bool]] | None = None,
        color: discord.Color | None = None,
    ) -> None:
        audit_cog = self.bot.get_cog("AuditLogCog")
        if audit_cog is None or not hasattr(audit_cog, "emit_external_event"):
            return
        await audit_cog.emit_external_event(
            guild_id,
            title=title,
            description=description,
            status="event",
            color=color or discord.Color.red(),
            user_name="ServerGuard",
            channel_name="ServerGuard",
            fields=fields,
        )

    async def _arm_feature_if_needed(
        self,
        guild_id: int,
        feature: str,
        *,
        duration_minutes: int,
        reason: str,
    ) -> bool:
        state = self.store.get_feature(guild_id, feature)
        if self._is_active(state):
            return False
        await self.enable_feature(guild_id, feature, duration_minutes=duration_minutes, reason=reason)
        return True

    async def _handle_threat_level_actions(
        self,
        guild_id: int,
        previous_level: str,
        current_level: str,
    ) -> None:
        state = self._threat_state[guild_id]
        action_notes = state["recent_actions"]

        if self._level_rank(current_level) >= self._level_rank("moderate") and self._level_rank(previous_level) < self._level_rank("moderate"):
            action_notes.appendleft("Staff alert triggered")
            await self._emit_threat_event(
                guild_id,
                title="ServerGuard threat raised",
                description="ServerGuard detected coordinated activity and moved the server into a moderate threat watch state.",
                fields=[("Threat level", "Moderate threat", True)],
                color=discord.Color.orange(),
            )
            state["last_notified_level"] = "moderate"

        if self._level_rank(current_level) >= self._level_rank("major") and self._level_rank(previous_level) < self._level_rank("major"):
            armed = []
            for feature in ("antispam", "mentionguard", "inviteblock", "linkblock"):
                if await self._arm_feature_if_needed(
                    guild_id,
                    feature,
                    duration_minutes=30,
                    reason="ServerGuard Guardian strict response",
                ):
                    armed.append(feature.replace("mentionguard", "mention guard"))
            action_notes.appendleft("Strict filtering engaged")
            await self._emit_threat_event(
                guild_id,
                title="ServerGuard major threat response",
                description="ServerGuard moved into strict filtering because several raid indicators started stacking together.",
                fields=[("Auto-armed", ", ".join(armed) if armed else "Strict filters were already active", False)],
                color=discord.Color.red(),
            )
            state["last_auto_level"] = "major"

        if self._level_rank(current_level) >= self._level_rank("high") and self._level_rank(previous_level) < self._level_rank("high"):
            anti_join_armed = await self._arm_feature_if_needed(
                guild_id,
                "antijoin",
                duration_minutes=30,
                    reason="ServerGuard Guardian high threat response",
            )
            action_notes.appendleft("Raid mode response active")
            await self._emit_threat_event(
                guild_id,
                title="ServerGuard high threat response",
                description="ServerGuard detected a likely raid pattern. Anti-join has been armed and staff should be ready to trigger lockdown if the pressure continues.",
                fields=[
                    ("Anti-join", "Enabled for 30 minutes" if anti_join_armed else "Already active", True),
                    ("Lockdown", "Recommended if chat pressure continues", False),
                ],
                color=discord.Color.red(),
            )
            state["last_auto_level"] = "high"
            state["raid_mode_active"] = True

    async def _record_threat_signal(
        self,
        guild: discord.Guild,
        *,
        signal_key: str,
        title: str,
        detail: str,
        points: int,
        cooldown_seconds: int,
    ) -> None:
        state = self.store.get_feature(guild.id, "antiraid")
        if not self._is_active(state):
            return

        now = utcnow()
        if not self._signal_ready(guild.id, signal_key, cooldown_seconds, now):
            return

        threat_state = self._apply_threat_decay(guild.id, now)
        previous_level = threat_state.get("current_level", "normal")
        threat_state["score"] = min(threat_state.get("score", 0.0) + points, MAX_THREAT_SCORE)
        threat_state["last_decay_at"] = now
        threat_state["recent_signals"].appendleft(
            {
                "title": title,
                "detail": detail,
                "points": points,
                "timestamp": now,
            }
        )
        current_level, _ = self._score_level(threat_state["score"])
        threat_state["current_level"] = current_level
        threat_state["raid_mode_active"] = current_level == "high"
        await self._handle_threat_level_actions(guild.id, previous_level, current_level)

    def get_dashboard_state(self, guild_id: int) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        active_count = 0

        for feature in DEFENSE_FEATURES:
            state = self.store.get_feature(guild_id, feature)
            if feature == "antiraid" and not self._guardian_available(guild_id):
                if state.get("enabled"):
                    state = self.store.patch_feature(guild_id, feature, enabled=False, ends_at=None)
                self._threat_state[guild_id] = self._new_threat_state()
            ends_at = from_iso(state.get("ends_at"))
            if state.get("enabled") and ends_at and ends_at <= utcnow() and feature != "lockdown":
                state = self.store.patch_feature(guild_id, feature, enabled=False, ends_at=None)
            if state.get("enabled"):
                active_count += 1
            payload[feature] = state

        payload["active_count"] = active_count
        return payload

    def get_threat_summary(self, guild_id: int) -> dict[str, Any]:
        threat_state = self._apply_threat_decay(guild_id)
        antiraid_state = self.store.get_feature(guild_id, "antiraid")
        guardian_available = self._guardian_available(guild_id)
        score = round(threat_state.get("score", 0.0), 1)
        level_key, level_label = self._score_level(score)

        if not guardian_available:
            level_key, level_label = ("normal", "Premium required")
        elif not self._is_active(antiraid_state):
            level_key, level_label = ("normal", "Offline")

        if not guardian_available:
            status_copy = "Upgrade this server to Premium to unlock Guardian threat scoring, raid presets, and automatic pressure tracking."
        elif level_key == "high":
            status_copy = "Guardian has moved into raid response mode. Anti-join should be live and staff should be ready to hard-lock channels if pressure continues."
        elif level_key == "major":
            status_copy = "Coordinated activity is stacking. Strict filtering should already be tightening links, invites, spam, and mention abuse."
        elif level_key == "moderate":
            status_copy = "ServerGuard is watching a suspicious pattern. Staff should keep an eye on joins, pings, and repeated content."
        elif self._is_active(antiraid_state):
            status_copy = "Guardian is live and watching for bursts, coordination, and weak-account patterns."
        else:
            status_copy = "Turn Guardian on when you want ServerGuard to watch for coordinated joins, spam waves, and escalating threat signals."

        recent_signals = []
        for signal in list(threat_state.get("recent_signals", [])):
            recent_signals.append(
                {
                    "title": signal["title"],
                    "detail": signal["detail"],
                    "points": signal["points"],
                    "timestamp": signal["timestamp"].strftime("%Y-%m-%d %H:%M:%S UTC"),
                }
            )

        bands = []
        for index, (band_key, band_label, start) in enumerate(THREAT_LEVELS):
            end = THREAT_LEVELS[index + 1][2] - 1 if index + 1 < len(THREAT_LEVELS) else MAX_THREAT_SCORE
            bands.append(
                {
                    "key": band_key,
                    "label": band_label,
                    "range": f"{start}-{end}" if band_key != "high" else "75+",
                    "reached": score >= start,
                    "active": level_key == band_key,
                }
            )

        next_threshold = next((threshold for _, _, threshold in THREAT_LEVELS[1:] if score < threshold), None)
        return {
            "enabled": guardian_available and self._is_active(antiraid_state),
            "available": guardian_available,
            "score": score,
            "score_display": f"{int(round(score))}/100",
            "level_key": level_key,
            "level_label": level_label,
            "progress_percent": max(0, min(int(round(score)), 100)),
            "status_copy": status_copy,
            "preset": antiraid_state.get("preset", "balanced"),
            "blocked_phrases": antiraid_state.get("blocked_phrases", []),
            "allowed_domains": antiraid_state.get("allowed_domains", []),
            "recent_signals": recent_signals,
            "recent_actions": list(threat_state.get("recent_actions", [])),
            "raid_mode_active": threat_state.get("raid_mode_active", False),
            "next_threshold": next_threshold,
            "bands": bands,
        }

    def build_dashboard_state(self, guild_id: int, role_lookup: dict[int, str] | None = None) -> dict[str, Any]:
        role_lookup = role_lookup or {}
        state = self.get_dashboard_state(guild_id)
        guardian_available = self._guardian_available(guild_id)

        def remaining_minutes(item: dict[str, Any]) -> int | None:
            ends_at = from_iso(item.get("ends_at"))
            if ends_at is None:
                return None
            delta = max(int((ends_at - utcnow()).total_seconds() // 60), 0)
            return delta or 1

        def remaining_label(item: dict[str, Any]) -> str:
            ends_at = from_iso(item.get("ends_at"))
            if ends_at is None:
                return "No timer"

            seconds_left = max(int((ends_at - utcnow()).total_seconds()), 0)
            if seconds_left < 60:
                return "Ends in <1 min"

            minutes_left = max(seconds_left // 60, 1)
            if minutes_left < 60:
                return f"Ends in {minutes_left}m"

            hours_left, rem_minutes = divmod(minutes_left, 60)
            if hours_left < 24:
                return f"Ends in {hours_left}h {rem_minutes}m" if rem_minutes else f"Ends in {hours_left}h"

            days_left, rem_hours = divmod(hours_left, 24)
            return f"Ends in {days_left}d {rem_hours}h" if rem_hours else f"Ends in {days_left}d"

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
            premium_locked = feature in {"antiraid", "lockdown"} and not guardian_available
            premium_label = {
                "antiraid": "Upgrade this server to unlock Guardian.",
                "lockdown": "Upgrade this server to unlock Lockdown.",
            }.get(feature)
            return {
                "name": feature,
                "title": title,
                "tag": tag,
                "description": description,
                "enabled": item.get("enabled", False),
                "locked": premium_locked,
                "duration_minutes": minutes_left,
                "duration_label": "Runs until disabled" if not item.get("ends_at") else f"{minutes_left} minute timer",
                "status_label": "Premium only" if premium_locked else ("Armed" if item.get("enabled") else "Offline"),
                "remaining_label": remaining_label(item),
                "remaining_premium_label": premium_label if premium_locked else None,
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
                "Catches rapid message bursts early, clears the burst, and cools the user down with a timeout.",
                rate_label="5 messages / 6 seconds",
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
                "Tracks mention bursts across messages so staff can stop ping raids before they spread.",
                rate_label="5 mentions / 10 seconds",
            ),
            make_card(
                "autofilter",
                "AutoFilter",
                "Blocked words",
                "Blocks flagged words or phrases, warns members up to three times, then times them out for one hour if they keep pushing it.",
                rate_label="3 warnings, then 60-minute timeout",
            ),
            make_card(
                "lockdown",
                "Lockdown",
                "Channel freeze",
                "Locks the server down and keeps selected talk roles moving while the server is under pressure.",
            ),
            make_card(
                "antiraid",
                "Guardian",
                "Threat scoring",
                "Scores suspicious bursts, fresh-account waves, repeated content, and stacked guard triggers so ServerGuard can escalate earlier.",
                rate_label="Live score + automatic response ladder",
            ),
        ]

        timed_count = len([card for card in cards if card["enabled"] and state[card["name"]].get("ends_at")])
        lockdown_roles = state["lockdown"].get("allowed_role_ids", [])
        threat_summary = self.get_threat_summary(guild_id)
        return {
            "cards": cards,
            "active_count": state["active_count"],
            "timed_count": timed_count,
            "lockdown_role_ids": lockdown_roles,
            "lockdown_role_names": [role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in lockdown_roles],
            "lockdown_role_count": len(lockdown_roles),
            "autofilter_terms": state["autofilter"].get("filter_terms", []),
            "autofilter_warning_limit": state["autofilter"].get("warning_limit", 3),
            "autofilter_timeout_minutes": state["autofilter"].get("timeout_minutes", 60),
            "threat": threat_summary,
        }

    def update_guardian_lists(
        self,
        guild_id: int,
        *,
        blocked_phrases: list[str] | None = None,
        allowed_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        current = self.store.get_feature(guild_id, "antiraid")
        next_phrases = [
            str(item).strip().lower()
            for item in (blocked_phrases if blocked_phrases is not None else current.get("blocked_phrases", []))
            if str(item).strip()
        ][:50]
        next_domains = [
            str(item).strip().lower()
            for item in (allowed_domains if allowed_domains is not None else current.get("allowed_domains", []))
            if str(item).strip()
        ][:50]
        return self.store.patch_feature(
            guild_id,
            "antiraid",
            blocked_phrases=next_phrases,
            allowed_domains=next_domains,
        )

    def update_autofilter_terms(self, guild_id: int, terms: list[str]) -> dict[str, Any]:
        cleaned = [
            str(item).strip().lower()
            for item in terms
            if str(item).strip()
        ][:100]
        return self.store.patch_feature(guild_id, "autofilter", filter_terms=cleaned)

    def apply_guardian_preset(self, guild_id: int, preset: str) -> dict[str, Any]:
        preset_name = str(preset).strip().lower()
        presets = {
            "balanced": {
                "antispam": {"message_limit": 5, "window_seconds": 6, "timeout_seconds": 90},
                "mentionguard": {"mention_limit": 5, "window_seconds": 10, "timeout_seconds": 90},
            },
            "strict": {
                "antispam": {"message_limit": 4, "window_seconds": 5, "timeout_seconds": 180},
                "mentionguard": {"mention_limit": 4, "window_seconds": 8, "timeout_seconds": 180},
            },
            "emergency": {
                "antispam": {"message_limit": 3, "window_seconds": 4, "timeout_seconds": 300},
                "mentionguard": {"mention_limit": 3, "window_seconds": 6, "timeout_seconds": 300},
            },
        }
        selected = presets.get(preset_name, presets["balanced"])
        self.store.patch_feature(guild_id, "antiraid", preset=preset_name if preset_name in presets else "balanced")
        self.store.patch_feature(guild_id, "antispam", **selected["antispam"])
        self.store.patch_feature(guild_id, "mentionguard", **selected["mentionguard"])
        return self.store.get_feature(guild_id, "antiraid")

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

    def _trim_message_bucket(
        self,
        bucket: deque[tuple[datetime, discord.Message]],
        now: datetime,
        window_seconds: int,
    ) -> None:
        while bucket and (now - bucket[0][0]).total_seconds() > window_seconds:
            bucket.popleft()

    def _trim_mention_bucket(
        self,
        bucket: deque[tuple[datetime, discord.Message, int]],
        now: datetime,
        window_seconds: int,
    ) -> None:
        while bucket and (now - bucket[0][0]).total_seconds() > window_seconds:
            bucket.popleft()

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

    async def _delete_messages(
        self,
        messages: list[discord.Message],
        warning_channel: discord.abc.Messageable,
        warning: str,
    ) -> None:
        seen_ids: set[int] = set()
        for buffered_message in messages:
            if buffered_message.id in seen_ids:
                continue
            seen_ids.add(buffered_message.id)
            try:
                await buffered_message.delete()
            except Exception:
                continue
        await self._warn_channel(warning_channel, warning)

    def _is_active(self, state: dict[str, Any]) -> bool:
        if not state.get("enabled"):
            return False
        ends_at = from_iso(state.get("ends_at"))
        return not (ends_at and ends_at <= utcnow())

    def _contains_link(self, text: str) -> bool:
        return bool(URL_PATTERN.search(text or ""))

    def _contains_invite(self, text: str) -> bool:
        return bool(INVITE_PATTERN.search(text or "")) or _is_discord_invite_text(text or "")

    def _mention_count(self, message: discord.Message) -> int:
        return len(message.mentions) + len(message.role_mentions) + int(message.mention_everyone)

    async def _record_module_trigger(self, guild: discord.Guild, module_name: str, detail: str) -> None:
        now = utcnow()
        bucket = self._module_trigger_windows[guild.id]
        bucket.append((now, module_name))
        self._trim_simple_window(bucket, now, 60)
        distinct_modules = {name for _, name in bucket}
        await self._record_threat_signal(
            guild,
            signal_key=f"guard-trigger-{module_name}",
            title=f"{module_name.title()} triggered",
            detail=detail,
            points=6,
            cooldown_seconds=45,
        )
        if len(distinct_modules) >= 2:
            await self._record_threat_signal(
                guild,
                signal_key="guard-stack",
                title="Multiple ServerGuard modules fired",
                detail="More than one protection tripped close together, which usually means the pressure is coordinated instead of random.",
                points=15,
                cooldown_seconds=60,
            )

    async def _record_join_risk(self, member: discord.Member) -> None:
        guild = member.guild
        now = utcnow()
        bucket = self._join_windows[guild.id]
        bucket.append(
            {
                "time": now,
                "user_id": member.id,
                "age_days": self._member_account_age_days(member),
                "default_profile": self._member_has_default_profile(member),
                "signature": self._name_signature(member.name or ""),
                "display_signature": self._name_signature(member.display_name or ""),
            }
        )
        self._trim_payload_window(bucket, now, 60)
        joins = list(bucket)

        if len({item["user_id"] for item in joins}) >= 10:
            await self._record_threat_signal(
                guild,
                signal_key="join-burst",
                title="Join burst detected",
                detail="A large number of members joined inside one minute.",
                points=10,
                cooldown_seconds=45,
            )

        fresh_accounts = [item for item in joins if item["age_days"] <= 7]
        if len({item["user_id"] for item in fresh_accounts}) >= 5:
            await self._record_threat_signal(
                guild,
                signal_key="fresh-account-burst",
                title="Fresh-account burst detected",
                detail="Several recently created accounts joined close together.",
                points=15,
                cooldown_seconds=60,
            )

        default_profiles = [item for item in joins if item["default_profile"]]
        if len({item["user_id"] for item in default_profiles}) >= 4:
            await self._record_threat_signal(
                guild,
                signal_key="default-profile-burst",
                title="Default-profile join wave",
                detail="Multiple joiners are still using default profile images, which often clusters during raids.",
                points=8,
                cooldown_seconds=90,
            )

        signatures: dict[str, set[int]] = defaultdict(set)
        for item in joins:
            for signature_key in (item["signature"], item["display_signature"]):
                if signature_key:
                    signatures[signature_key].add(item["user_id"])
        if any(len(user_ids) >= 4 for user_ids in signatures.values()):
            await self._record_threat_signal(
                guild,
                signal_key="name-pattern-burst",
                title="Similar-name pattern burst",
                detail="Several joiners share nearly the same account or display-name pattern.",
                points=12,
                cooldown_seconds=90,
            )

        last_leave = self._member_leave_times.get((guild.id, member.id))
        if last_leave and (now - last_leave) <= timedelta(minutes=RECENT_REJOIN_MINUTES):
            await self._record_threat_signal(
                guild,
                signal_key=f"rejoin-{member.id}",
                title="Fast rejoin detected",
                detail=f"{member} left and rejoined quickly, which can be part of churn-based raid pressure.",
                points=8,
                cooldown_seconds=300,
            )

    async def _record_leave_risk(self, member: discord.Member) -> None:
        guild = member.guild
        now = utcnow()
        self._member_leave_times[(guild.id, member.id)] = now
        bucket = self._leave_windows[guild.id]
        bucket.append({"time": now, "user_id": member.id})
        self._trim_payload_window(bucket, now, 900)
        churn_users = {item["user_id"] for item in bucket}
        if len(churn_users) >= 4:
            await self._record_threat_signal(
                guild,
                signal_key="leave-rejoin-churn",
                title="Leave and rejoin churn",
                detail="Several accounts have been leaving or rejoining in a short window.",
                points=10,
                cooldown_seconds=120,
            )

    async def _record_message_risk(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or not isinstance(message.author, discord.Member):
            return

        now = utcnow()
        mention_count = self._mention_count(message)
        entry = {
            "time": now,
            "user_id": message.author.id,
            "channel_id": getattr(message.channel, "id", 0),
            "content_key": self._normalize_text_signature(message.content or ""),
            "has_link": self._contains_link(message.content or ""),
            "has_invite": self._contains_invite(message.content or ""),
            "mention_count": mention_count,
            "staff_ping": self._staff_role_pinged(message),
            "recent_join": self._member_is_recent_join(message.author),
        }
        bucket = self._guild_message_windows[guild.id]
        bucket.append(entry)
        self._trim_payload_window(bucket, now, 60)

        recent_messages = [item for item in bucket if (now - item["time"]).total_seconds() <= 15]
        if len(recent_messages) >= 12 and len({item["user_id"] for item in recent_messages}) >= 5:
            await self._record_threat_signal(
                guild,
                signal_key="message-burst",
                title="Message burst detected",
                detail="Several accounts started posting rapidly across the server.",
                points=12,
                cooldown_seconds=30,
            )

        recent_30 = [item for item in bucket if (now - item["time"]).total_seconds() <= 30]
        repeated_content: dict[str, set[int]] = defaultdict(set)
        for item in recent_30:
            if len(item["content_key"]) >= 8:
                repeated_content[item["content_key"]].add(item["user_id"])
        for content_key, user_ids in repeated_content.items():
            if len(user_ids) >= 3:
                await self._record_threat_signal(
                    guild,
                    signal_key=f"repeat-{content_key[:32]}",
                    title="Repeated content burst",
                    detail="Multiple members posted the same or near-identical content inside a short window.",
                    points=20,
                    cooldown_seconds=45,
                )
                break

        mention_window = [item for item in bucket if (now - item["time"]).total_seconds() <= 20 and item["mention_count"] > 0]
        if sum(item["mention_count"] for item in mention_window) >= 10 and len({item["user_id"] for item in mention_window}) >= 3:
            await self._record_threat_signal(
                guild,
                signal_key="mention-burst",
                title="Mention burst detected",
                detail="Several messages are stacking member, role, or everyone mentions unusually fast.",
                points=15,
                cooldown_seconds=45,
            )

        link_window = [item for item in bucket if (now - item["time"]).total_seconds() <= 20 and item["has_link"]]
        if len(link_window) >= 4 and len({item["user_id"] for item in link_window}) >= 3:
            await self._record_threat_signal(
                guild,
                signal_key="link-burst",
                title="Link burst detected",
                detail="Multiple members sent links in a short span, which is a common raid or scam signal.",
                points=15,
                cooldown_seconds=45,
            )

        invite_window = [item for item in bucket if (now - item["time"]).total_seconds() <= 20 and item["has_invite"]]
        if len(invite_window) >= 3 and len({item["user_id"] for item in invite_window}) >= 2:
            await self._record_threat_signal(
                guild,
                signal_key="invite-burst",
                title="Invite spam burst",
                detail="Discord invite links started showing up across multiple accounts.",
                points=20,
                cooldown_seconds=45,
            )

        channel_spread: dict[int, set[int]] = defaultdict(set)
        for item in recent_30:
            channel_spread[item["user_id"]].add(item["channel_id"])
        for user_id, channels in channel_spread.items():
            if len(channels) >= 4:
                await self._record_threat_signal(
                    guild,
                    signal_key=f"channel-spread-{user_id}",
                    title="Channel spread spam",
                    detail="One account started spraying messages across several channels quickly.",
                    points=12,
                    cooldown_seconds=45,
                )
                break

        staff_ping_window = [
            item
            for item in bucket
            if (now - item["time"]).total_seconds() <= 20 and item["staff_ping"] and item["recent_join"]
        ]
        if len({item["user_id"] for item in staff_ping_window}) >= 3:
            await self._record_threat_signal(
                guild,
                signal_key="staff-role-targeting",
                title="Staff-role targeting",
                detail="Recently joined members started pinging staff roles unusually fast.",
                points=15,
                cooldown_seconds=60,
            )

    async def _record_reaction_risk(self, reaction: discord.Reaction, user: discord.abc.User | discord.Member) -> None:
        if not isinstance(user, discord.Member) or user.bot:
            return
        guild = user.guild
        now = utcnow()
        bucket = self._reaction_windows[guild.id]
        bucket.append(
            {
                "time": now,
                "user_id": user.id,
                "recent_join": self._member_is_recent_join(user),
            }
        )
        self._trim_payload_window(bucket, now, 20)
        fresh_reactors = [item for item in bucket if item["recent_join"]]
        if len(fresh_reactors) >= 12 and len({item["user_id"] for item in fresh_reactors}) >= 6:
            await self._record_threat_signal(
                guild,
                signal_key="reaction-swarm",
                title="Reaction swarm",
                detail="A cluster of recently joined accounts started reacting in a burst.",
                points=10,
                cooldown_seconds=45,
            )

    async def process_message(self, message: discord.Message) -> bool:
        if message.guild is None or not isinstance(message.author, discord.Member):
            return False
        if message.author.bot or self._member_is_exempt(message.author):
            return False

        guild_id = message.guild.id
        content = message.content or ""
        antiraid = self.store.get_feature(guild_id, "antiraid")
        antiraid_active = self._guardian_available(guild_id) and self._is_active(antiraid)
        allowed_domains = set(antiraid.get("allowed_domains", []))
        blocked_phrases = [phrase for phrase in antiraid.get("blocked_phrases", []) if phrase]
        autofilter = self.store.get_feature(guild_id, "autofilter")
        autofilter_terms = [term for term in autofilter.get("filter_terms", []) if term]

        if antiraid_active:
            await self._record_message_risk(message)
            lowered = content.lower()
            if blocked_phrases and any(phrase in lowered for phrase in blocked_phrases):
                await self._delete_message(message, "Guardian removed a blocked phrase.")
                await self._record_module_trigger(message.guild, "guardian blacklist", "Guardian removed a blocked phrase during active monitoring.")
                return True

        if self._is_active(autofilter):
            lowered = content.lower()
            matched_term = next((term for term in autofilter_terms if term in lowered), None)
            if matched_term:
                await self._delete_message(message, "AutoFilter blocked that message.")
                warning_counts = dict(autofilter.get("warning_counts", {}))
                user_key = str(message.author.id)
                warning_count = int(warning_counts.get(user_key, 0)) + 1
                warning_counts[user_key] = warning_count
                self.store.patch_feature(guild_id, "autofilter", warning_counts=warning_counts)
                warning_limit = int(autofilter.get("warning_limit", 3))
                timeout_minutes = int(autofilter.get("timeout_minutes", 60))
                if warning_count > warning_limit:
                    await self._timeout_member(
                        message.author,
                        timeout_minutes * 60,
                        "AutoFilter repeated violations.",
                    )
                    await self._warn_channel(
                        message.channel,
                        f"{message.author.mention} triggered AutoFilter again and has been timed out for {timeout_minutes} minutes.",
                    )
                else:
                    remaining = max(warning_limit - warning_count, 0)
                    await self._warn_channel(
                        message.channel,
                        f"{message.author.mention}, that word or phrase is blocked here. Warning {warning_count}/{warning_limit}.{f' {remaining} warning(s) left before a timeout.' if remaining else ''}",
                    )
                if antiraid_active:
                    await self._record_module_trigger(message.guild, "autofilter", "AutoFilter blocked a flagged word or phrase during active monitoring.")
                return True

        linkblock = self.store.get_feature(guild_id, "linkblock")
        domains = set(_extract_domains(content))
        whitelisted = bool(domains and any(domain in allowed_domains or any(domain.endswith(f".{allowed}") for allowed in allowed_domains) for domain in domains))
        if self._is_active(linkblock) and self._contains_link(content) and not self._contains_invite(content) and not whitelisted:
            await self._delete_message(message, "Link Block prevented that message.")
            if antiraid_active:
                await self._record_module_trigger(message.guild, "linkblock", "Link Block removed a message while Guardian was watching the server.")
            return True

        inviteblock = self.store.get_feature(guild_id, "inviteblock")
        if self._is_active(inviteblock) and self._contains_invite(content) and not whitelisted:
            await self._delete_message(message, "Invite Block prevented that message.")
            if antiraid_active:
                await self._record_module_trigger(message.guild, "inviteblock", "Invite Block removed a Discord invite while Guardian was watching the server.")
            return True

        mentionguard = self.store.get_feature(guild_id, "mentionguard")
        mention_limit = int(mentionguard.get("mention_limit", 5))

        antispam = self.store.get_feature(guild_id, "antispam")
        if self._is_active(antispam):
            bucket = self._spam_windows[(guild_id, message.author.id)]
            now = utcnow()
            bucket.append((now, message))
            window_seconds = int(antispam.get("window_seconds", 6))
            message_limit = int(antispam.get("message_limit", 5))
            self._trim_message_bucket(bucket, now, window_seconds)
            if len(bucket) >= message_limit:
                burst_messages = [item[1] for item in bucket]
                await self._delete_messages(
                    burst_messages,
                    message.channel,
                    f"Anti-spam cleared a {message_limit}-message burst in {window_seconds} seconds.",
                )
                await self._timeout_member(
                    message.author,
                    int(antispam.get("timeout_seconds", 90)),
                    "Anti-spam triggered.",
                )
                if antiraid_active:
                    await self._record_module_trigger(message.guild, "antispam", "Anti-spam had to clear a rapid burst of messages.")
                bucket.clear()
                return True

        if self._is_active(mentionguard):
            mention_count = self._mention_count(message)
            if mention_count > 0:
                bucket = self._mention_windows[(guild_id, message.author.id)]
                now = utcnow()
                bucket.append((now, message, mention_count))
                window_seconds = int(mentionguard.get("window_seconds", 10))
                self._trim_mention_bucket(bucket, now, window_seconds)
                total_mentions = sum(item[2] for item in bucket)
                if total_mentions >= mention_limit:
                    burst_messages = [item[1] for item in bucket]
                    await self._delete_messages(
                        burst_messages,
                        message.channel,
                        f"Mention Guard cleared a burst of {total_mentions} mentions in {window_seconds} seconds.",
                    )
                    await self._timeout_member(
                        message.author,
                        int(mentionguard.get("timeout_seconds", 90)),
                        "Mention Guard triggered.",
                    )
                    if antiraid_active:
                        await self._record_module_trigger(message.guild, "mentionguard", "Mention Guard had to clear a rapid burst of mentions.")
                    bucket.clear()
                    return True

        return False

    async def handle_member_join(self, member: discord.Member) -> bool:
        if self._guardian_available(member.guild.id) and self._is_active(self.store.get_feature(member.guild.id, "antiraid")):
            await self._record_join_risk(member)

        antijoin = self.store.get_feature(member.guild.id, "antijoin")
        if not self._is_active(antijoin):
            return False

        me = member.guild.me
        if me is None or not me.guild_permissions.kick_members:
            return False

        try:
            await member.kick(reason="Anti-join is active.")
            if self._guardian_available(member.guild.id) and self._is_active(self.store.get_feature(member.guild.id, "antiraid")):
                await self._record_module_trigger(member.guild, "antijoin", "Anti-join removed a fresh join during a high-pressure window.")
            return True
        except Exception:
            return False

    async def handle_member_remove(self, member: discord.Member) -> None:
        if self._guardian_available(member.guild.id) and self._is_active(self.store.get_feature(member.guild.id, "antiraid")):
            await self._record_leave_risk(member)

    async def handle_reaction_add(self, reaction: discord.Reaction, user: discord.abc.User | discord.Member) -> None:
        guild = getattr(getattr(reaction, "message", None), "guild", None)
        if guild is None:
            return
        if self._guardian_available(guild.id) and self._is_active(self.store.get_feature(guild.id, "antiraid")):
            await self._record_reaction_risk(reaction, user)

    async def enable_feature(
        self,
        guild_id: int,
        feature: str,
        *,
        duration_minutes: int | None = None,
        actor: discord.abc.User | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if feature in {"antiraid", "lockdown"} and not self._guardian_available(guild_id):
            feature_name = {"antiraid": "Guardian", "lockdown": "Lockdown"}[feature]
            raise PermissionError(f"{feature_name} is part of ServerCore Premium right now.")
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
        if feature == "antiraid":
            self._threat_state[guild_id] = self._new_threat_state()
        if feature == "autofilter":
            return self.store.patch_feature(guild_id, feature, enabled=False, ends_at=None, warning_counts={})
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
            await self._refresh_lockdown_roles(guild_id)
        return updated

    async def ensure_lockdown_roles(self, guild_id: int, role_ids: list[int]) -> list[int]:
        updated = await self.set_lockdown_roles(guild_id, role_ids)
        return list(updated.get("allowed_role_ids", []))

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
        if normalized in {"antiraid", "lockdown"} and enabled and not self._guardian_available(guild_id):
            feature_name = {"antiraid": "Guardian", "lockdown": "Lockdown"}[normalized]
            raise PermissionError(f"{feature_name} is part of ServerCore Premium right now.")
        current = self.store.get_feature(guild_id, normalized)
        if enabled:
            if normalized == "lockdown" and current.get("enabled"):
                updated_state = await self.set_duration(guild_id, normalized, duration_minutes)
                snapshot = updated_state.get("snapshot", {})
                if snapshot:
                    await self._refresh_lockdown_roles(guild_id)
                    return self.store.get_feature(guild_id, normalized)
                return await self.enable_feature(guild_id, normalized, duration_minutes=duration_minutes)
            if current.get("enabled"):
                return await self.set_duration(guild_id, normalized, duration_minutes)
            return await self.enable_feature(guild_id, normalized, duration_minutes=duration_minutes)
        return await self.disable_feature(guild_id, normalized)

    def reset_threat_state(self, guild_id: int) -> dict[str, Any]:
        self._threat_state[guild_id] = self._new_threat_state()
        for key in list(self._threat_cooldowns):
            if key[0] == guild_id:
                self._threat_cooldowns.pop(key, None)
        self._guild_message_windows.pop(guild_id, None)
        self._join_windows.pop(guild_id, None)
        self._leave_windows.pop(guild_id, None)
        self._reaction_windows.pop(guild_id, None)
        self._module_trigger_windows.pop(guild_id, None)
        return self.get_threat_summary(guild_id)

    async def enable_all(
        self,
        guild_id: int,
        *,
        duration_minutes: int | None = None,
        actor: discord.abc.User | None = None,
        reason: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for feature in DEFENSE_FEATURES:
            if feature == "antiraid" and not self._guardian_available(guild_id):
                results[feature] = self.store.get_feature(guild_id, feature)
                continue
            results[feature] = await self.enable_feature(
                guild_id,
                feature,
                duration_minutes=duration_minutes,
                actor=actor,
                reason=reason,
            )
        return results

    async def disable_all(
        self,
        guild_id: int,
        *,
        actor: discord.abc.User | None = None,
        reason: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for feature in DEFENSE_FEATURES:
            results[feature] = await self.disable_feature(
                guild_id,
                feature,
                actor=actor,
                reason=reason,
            )
        return results

    def is_enabled(self, guild_id: int, feature: str) -> bool:
        normalized = "mentionguard" if feature == "mentionblock" else feature
        if normalized not in DEFENSE_FEATURES:
            return False
        if normalized == "antiraid" and not self._guardian_available(guild_id):
            return False
        return self._is_active(self.store.get_feature(guild_id, normalized))

    async def _refresh_lockdown_roles(self, guild_id: int) -> None:
        state = self.store.get_feature(guild_id, "lockdown")
        snapshot = state.get("snapshot", {})
        guild = self.bot.get_guild(guild_id)
        if guild is None or not snapshot:
            return

        allowed_role_ids = set(state.get("allowed_role_ids", []))
        updated_snapshot = copy.deepcopy(snapshot)
        for channel_id, channel_snapshot in updated_snapshot.items():
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel):
                continue

            tracked_role_states = channel_snapshot.setdefault("roles", {})
            tracked_member_states = channel_snapshot.setdefault("members", {})
            for target in channel.overwrites:
                if isinstance(target, discord.Role) and target != guild.default_role:
                    tracked_role_states.setdefault(str(target.id), self._capture_lockdown_permissions(channel, target))
                elif isinstance(target, discord.Member):
                    tracked_member_states.setdefault(str(target.id), self._capture_lockdown_permissions(channel, target))

            for role_id in allowed_role_ids:
                role = guild.get_role(role_id)
                if role is not None and role != guild.default_role:
                    tracked_role_states.setdefault(str(role.id), self._capture_lockdown_permissions(channel, role))

            await self._set_channel_lockdown_permissions(
                channel,
                guild.default_role,
                self._lockdown_deny_overrides(),
                reason="Refreshing lockdown role permissions.",
            )

            for role_id_str in list(tracked_role_states):
                role_id = int(role_id_str)
                role = guild.get_role(role_id)
                if role is None:
                    continue
                await self._set_channel_lockdown_permissions(
                    channel,
                    role,
                    self._lockdown_allow_overrides() if role_id in allowed_role_ids else self._lockdown_deny_overrides(),
                    reason="Refreshing lockdown role permissions.",
                )

            for member_id_str in list(tracked_member_states):
                member = guild.get_member(int(member_id_str))
                if member is None:
                    continue
                await self._set_channel_lockdown_permissions(
                    channel,
                    member,
                    self._lockdown_allow_overrides()
                    if self._member_can_talk_during_lockdown(member, allowed_role_ids)
                    else self._lockdown_deny_overrides(),
                    reason="Refreshing lockdown member permissions.",
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
                "default_role": self._capture_lockdown_permissions(channel, guild.default_role),
                "roles": {},
                "members": {},
            }
            for target in channel.overwrites:
                if isinstance(target, discord.Role) and target != guild.default_role:
                    channel_snapshot["roles"][str(target.id)] = self._capture_lockdown_permissions(channel, target)
                elif isinstance(target, discord.Member):
                    channel_snapshot["members"][str(target.id)] = self._capture_lockdown_permissions(channel, target)

            for role_id in allowed_role_ids:
                role = guild.get_role(role_id)
                if role is not None and role != guild.default_role:
                    channel_snapshot["roles"].setdefault(str(role.id), self._capture_lockdown_permissions(channel, role))

            await self._set_channel_lockdown_permissions(
                channel,
                guild.default_role,
                self._lockdown_deny_overrides(),
                reason=reason or f"Server lockdown enabled by {actor or 'ServerDefense'}",
            )

            for role_id_str in channel_snapshot["roles"]:
                role = guild.get_role(int(role_id_str))
                if role is None:
                    continue
                await self._set_channel_lockdown_permissions(
                    channel,
                    role,
                    self._lockdown_allow_overrides()
                    if role.id in allowed_role_ids
                    else self._lockdown_deny_overrides(),
                    reason=reason or "Server lockdown role update.",
                )

            for member_id_str in channel_snapshot["members"]:
                member = guild.get_member(int(member_id_str))
                if member is None:
                    continue
                await self._set_channel_lockdown_permissions(
                    channel,
                    member,
                    self._lockdown_allow_overrides()
                    if self._member_can_talk_during_lockdown(member, allowed_role_ids)
                    else self._lockdown_deny_overrides(),
                    reason=reason or "Server lockdown member update.",
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
                default_snapshot = channel_snapshot.get("default_role")
                if default_snapshot is None:
                    default_snapshot = {"send_messages": channel_snapshot.get("default_send_messages")}
                await self._set_channel_lockdown_permissions(
                    channel,
                    guild.default_role,
                    default_snapshot,
                    reason=reason or f"Server lockdown disabled by {actor or 'ServerDefense'}",
                )
                for role_id_str, send_state in channel_snapshot.get("roles", {}).items():
                    role = guild.get_role(int(role_id_str))
                    if role is None:
                        continue
                    await self._set_channel_lockdown_permissions(
                        channel,
                        role,
                        send_state,
                        reason=reason or "Restoring lockdown role overwrite.",
                    )
                for member_id_str, send_state in channel_snapshot.get("members", {}).items():
                    member = guild.get_member(int(member_id_str))
                    if member is None:
                        continue
                    await self._set_channel_lockdown_permissions(
                        channel,
                        member,
                        send_state,
                        reason=reason or "Restoring lockdown member overwrite.",
                    )

        return self.store.patch_feature(
            guild_id,
            "lockdown",
            enabled=False,
            ends_at=None,
            snapshot={},
        )

    def _capture_lockdown_permissions(
        self,
        channel: discord.TextChannel,
        target: discord.Role | discord.Member,
    ) -> dict[str, bool | None]:
        overwrite = channel.overwrites_for(target)
        return {
            permission_key: getattr(overwrite, permission_key, None)
            for permission_key in LOCKDOWN_PERMISSION_KEYS
        }

    def _member_can_talk_during_lockdown(self, member: discord.Member, allowed_role_ids: set[int]) -> bool:
        return any(role.id in allowed_role_ids for role in member.roles)

    def _lockdown_deny_overrides(self) -> dict[str, bool]:
        return {permission_key: False for permission_key in LOCKDOWN_PERMISSION_KEYS}

    def _lockdown_allow_overrides(self) -> dict[str, bool]:
        return {
            "send_messages": True,
            "send_messages_in_threads": True,
            "add_reactions": True,
            "create_public_threads": True,
            "create_private_threads": True,
        }

    async def _set_channel_lockdown_permissions(
        self,
        channel: discord.TextChannel,
        target: discord.Role | discord.Member,
        permissions: dict[str, bool | None] | bool | None,
        *,
        reason: str | None = None,
    ) -> None:
        overwrite = channel.overwrites_for(target)
        if isinstance(permissions, dict):
            for permission_key in LOCKDOWN_PERMISSION_KEYS:
                if permission_key in permissions:
                    setattr(overwrite, permission_key, permissions[permission_key])
        else:
            overwrite.send_messages = permissions
        try:
            await channel.set_permissions(target, overwrite=overwrite, reason=reason)
        except Exception:
            return
