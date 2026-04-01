from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.storage import read_json, write_json


AUTOFEED_DATA_PATH = Path("autofeeds.json")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return None


class AutoFeedStore:
    def __init__(self, path: Path | None = None):
        self.path = path or AUTOFEED_DATA_PATH
        self._data = read_json(self.path, {"guilds": {}})

    def _save(self) -> None:
        write_json(self.path, self._data)

    def _ensure_guild(self, guild_id: int) -> dict[str, Any]:
        key = str(guild_id)
        if key not in self._data["guilds"]:
            self._data["guilds"][key] = {
                "counter": 0,
                "feeds": {},
            }
        return self._data["guilds"][key]

    def create_feed(
        self,
        guild_id: int,
        *,
        channel_id: int,
        created_by_id: int,
        created_by_name: str,
        message: str,
        interval_minutes: int,
    ) -> dict[str, Any]:
        guild = self._ensure_guild(guild_id)
        guild["counter"] = int(guild.get("counter", 0)) + 1
        feed_id = guild["counter"]
        next_post_at = utcnow() + timedelta(minutes=interval_minutes)
        record = {
            "id": feed_id,
            "guild_id": int(guild_id),
            "channel_id": int(channel_id),
            "created_by_id": int(created_by_id),
            "created_by_name": str(created_by_name),
            "message": str(message)[:1800],
            "interval_minutes": int(interval_minutes),
            "enabled": True,
            "created_at": to_iso(utcnow()),
            "last_posted_at": None,
            "next_post_at": to_iso(next_post_at),
        }
        guild["feeds"][str(feed_id)] = record
        self._save()
        return dict(record)

    def update_feed(
        self,
        guild_id: int,
        feed_id: int,
        *,
        channel_id: int | None | object = ...,
        message: str | None | object = ...,
        interval_minutes: int | None | object = ...,
    ) -> dict[str, Any] | None:
        feed = self._ensure_guild(guild_id)["feeds"].get(str(feed_id))
        if not isinstance(feed, dict):
            return None
        if channel_id is not ...:
            feed["channel_id"] = int(channel_id)
        if message is not ...:
            feed["message"] = str(message or "").strip()[:1800]
        if interval_minutes is not ...:
            feed["interval_minutes"] = int(interval_minutes)
            feed["next_post_at"] = to_iso(utcnow() + timedelta(minutes=int(interval_minutes)))
        self._save()
        return dict(feed)

    def list_feeds(self, guild_id: int, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        feeds = [
            dict(item)
            for item in self._ensure_guild(guild_id)["feeds"].values()
            if isinstance(item, dict) and (not enabled_only or item.get("enabled"))
        ]
        feeds.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return feeds

    def all_enabled(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for guild_key in self._data.get("guilds", {}):
            guild_id = int(guild_key)
            rows.extend(self.list_feeds(guild_id, enabled_only=True))
        return rows

    def get_feed(self, guild_id: int, feed_id: int) -> dict[str, Any] | None:
        item = self._ensure_guild(guild_id)["feeds"].get(str(feed_id))
        return dict(item) if isinstance(item, dict) else None

    def set_enabled(self, guild_id: int, feed_id: int, enabled: bool) -> dict[str, Any] | None:
        feed = self._ensure_guild(guild_id)["feeds"].get(str(feed_id))
        if not isinstance(feed, dict):
            return None
        feed["enabled"] = bool(enabled)
        if enabled and not feed.get("next_post_at"):
            feed["next_post_at"] = to_iso(utcnow() + timedelta(minutes=int(feed.get("interval_minutes", 60))))
        self._save()
        return dict(feed)

    def delete_feed(self, guild_id: int, feed_id: int) -> bool:
        removed = self._ensure_guild(guild_id)["feeds"].pop(str(feed_id), None)
        self._save()
        return isinstance(removed, dict)

    def update_after_post(self, guild_id: int, feed_id: int) -> dict[str, Any] | None:
        feed = self._ensure_guild(guild_id)["feeds"].get(str(feed_id))
        if not isinstance(feed, dict):
            return None
        now = utcnow()
        feed["last_posted_at"] = to_iso(now)
        feed["next_post_at"] = to_iso(now + timedelta(minutes=int(feed.get("interval_minutes", 60))))
        self._save()
        return dict(feed)
