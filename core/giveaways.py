from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage import read_json, write_json


GIVEAWAY_DATA_PATH = Path("giveaways.json")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return None


class GiveawayStore:
    def __init__(self, path: Path | None = None):
        self.path = path or GIVEAWAY_DATA_PATH
        self._data = read_json(self.path, {"guilds": {}})

    def _save(self) -> None:
        write_json(self.path, self._data)

    def _guild_key(self, guild_id: int) -> str:
        return str(guild_id)

    def _ensure_guild(self, guild_id: int) -> dict[str, Any]:
        key = self._guild_key(guild_id)
        if key not in self._data["guilds"]:
            self._data["guilds"][key] = {
                "counter": 0,
                "items": {},
            }
        return self._data["guilds"][key]

    def create_giveaway(
        self,
        guild_id: int,
        *,
        channel_id: int,
        host_id: int,
        host_name: str,
        prize: str,
        description: str | None,
        winner_count: int,
        ends_at: datetime,
        required_role_ids: list[int] | None = None,
        bonus_role_ids: list[int] | None = None,
        bonus_entries: int = 0,
    ) -> dict[str, Any]:
        guild_state = self._ensure_guild(guild_id)
        guild_state["counter"] = int(guild_state.get("counter", 0)) + 1
        giveaway_id = guild_state["counter"]
        record = {
            "id": giveaway_id,
            "guild_id": int(guild_id),
            "channel_id": int(channel_id),
            "message_id": None,
            "host_id": int(host_id),
            "host_name": str(host_name),
            "prize": str(prize)[:200],
            "description": (description or "").strip()[:600],
            "winner_count": int(winner_count),
            "entrants": [],
            "entry_counts": {},
            "required_role_ids": [int(role_id) for role_id in (required_role_ids or [])],
            "bonus_role_ids": [int(role_id) for role_id in (bonus_role_ids or [])],
            "bonus_entries": max(0, int(bonus_entries)),
            "status": "active",
            "created_at": utcnow_iso(),
            "ends_at": ends_at.astimezone(timezone.utc).isoformat(),
            "ended_at": None,
            "winner_ids": [],
            "winner_names": [],
        }
        guild_state["items"][str(giveaway_id)] = record
        self._save()
        return dict(record)

    def set_message_id(self, guild_id: int, giveaway_id: int, message_id: int) -> dict[str, Any] | None:
        record = self._ensure_guild(guild_id)["items"].get(str(giveaway_id))
        if not isinstance(record, dict):
            return None
        record["message_id"] = int(message_id)
        self._save()
        return dict(record)

    def get_giveaway(self, guild_id: int, giveaway_id: int) -> dict[str, Any] | None:
        record = self._ensure_guild(guild_id)["items"].get(str(giveaway_id))
        return dict(record) if isinstance(record, dict) else None

    def get_giveaway_by_message(self, guild_id: int, message_id: int) -> dict[str, Any] | None:
        for record in self._ensure_guild(guild_id)["items"].values():
            if isinstance(record, dict) and int(record.get("message_id") or 0) == int(message_id):
                return dict(record)
        return None

    def list_giveaways(self, guild_id: int, *, status: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        items = [
            dict(item)
            for item in self._ensure_guild(guild_id)["items"].values()
            if isinstance(item, dict) and (status is None or item.get("status") == status)
        ]
        items.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return items[:limit]

    def all_active(self) -> list[dict[str, Any]]:
        active: list[dict[str, Any]] = []
        for guild_key in self._data.get("guilds", {}):
            guild_state = self._ensure_guild(int(guild_key))
            for record in guild_state["items"].values():
                if isinstance(record, dict) and record.get("status") == "active":
                    active.append(dict(record))
        active.sort(key=lambda item: item.get("ends_at", ""))
        return active

    def add_entry(self, guild_id: int, giveaway_id: int, user_id: int) -> tuple[dict[str, Any] | None, bool]:
        return self.add_entries(guild_id, giveaway_id, user_id, entry_count=1)

    def add_entries(
        self,
        guild_id: int,
        giveaway_id: int,
        user_id: int,
        *,
        entry_count: int = 1,
    ) -> tuple[dict[str, Any] | None, bool]:
        record = self._ensure_guild(guild_id)["items"].get(str(giveaway_id))
        if not isinstance(record, dict):
            return None, False
        entrants = [int(item) for item in record.get("entrants", []) if str(item).isdigit()]
        if int(user_id) in entrants:
            return dict(record), False
        entrants.append(int(user_id))
        record["entrants"] = entrants
        entry_counts = {
            str(key): int(value)
            for key, value in (record.get("entry_counts") or {}).items()
            if str(key).isdigit()
        }
        entry_counts[str(int(user_id))] = max(1, int(entry_count))
        record["entry_counts"] = entry_counts
        self._save()
        return dict(record), True

    def remove_entry(self, guild_id: int, giveaway_id: int, user_id: int) -> dict[str, Any] | None:
        record = self._ensure_guild(guild_id)["items"].get(str(giveaway_id))
        if not isinstance(record, dict):
            return None
        entrants = [int(item) for item in record.get("entrants", []) if int(item) != int(user_id)]
        record["entrants"] = entrants
        entry_counts = record.get("entry_counts") or {}
        entry_counts.pop(str(int(user_id)), None)
        record["entry_counts"] = entry_counts
        self._save()
        return dict(record)

    def end_giveaway(
        self,
        guild_id: int,
        giveaway_id: int,
        *,
        winner_ids: list[int],
        winner_names: list[str],
    ) -> dict[str, Any] | None:
        record = self._ensure_guild(guild_id)["items"].get(str(giveaway_id))
        if not isinstance(record, dict):
            return None
        record["status"] = "ended"
        record["ended_at"] = utcnow_iso()
        record["winner_ids"] = [int(item) for item in winner_ids]
        record["winner_names"] = [str(item) for item in winner_names]
        self._save()
        return dict(record)

    def reroll_giveaway(
        self,
        guild_id: int,
        giveaway_id: int,
        *,
        winner_names: dict[int, str],
    ) -> dict[str, Any] | None:
        record = self._ensure_guild(guild_id)["items"].get(str(giveaway_id))
        if not isinstance(record, dict):
            return None
        entrants = [int(item) for item in record.get("entrants", []) if str(item).isdigit()]
        if not entrants:
            return None

        previous_winners = {int(item) for item in record.get("winner_ids", []) if str(item).isdigit()}
        pool = [entrant for entrant in entrants if entrant not in previous_winners] or entrants
        winner_count = max(1, min(int(record.get("winner_count", 1)), len(pool)))
        picked = random.sample(pool, winner_count)
        record["winner_ids"] = picked
        record["winner_names"] = [winner_names.get(item, f"User {item}") for item in picked]
        self._save()
        return dict(record)

    def choose_winners(self, record: dict[str, Any], *, name_lookup: dict[int, str]) -> tuple[list[int], list[str]]:
        entrants = [int(item) for item in record.get("entrants", []) if str(item).isdigit()]
        if not entrants:
            return [], []
        winner_count = max(1, min(int(record.get("winner_count", 1)), len(entrants)))
        entry_counts = {
            int(user_id): max(1, int(count))
            for user_id, count in (record.get("entry_counts") or {}).items()
            if str(user_id).isdigit()
        }
        picked = self._weighted_unique_sample(entrants, winner_count, entry_counts)
        names = [name_lookup.get(item, f"User {item}") for item in picked]
        return picked, names

    @staticmethod
    def _weighted_unique_sample(entrants: list[int], winner_count: int, entry_counts: dict[int, int]) -> list[int]:
        pool = list(dict.fromkeys(int(item) for item in entrants))
        winners: list[int] = []
        while pool and len(winners) < winner_count:
            weights = [max(1, int(entry_counts.get(user_id, 1))) for user_id in pool]
            picked = random.choices(pool, weights=weights, k=1)[0]
            winners.append(picked)
            pool = [user_id for user_id in pool if user_id != picked]
        return winners
