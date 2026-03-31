from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage import read_json, write_json


TICKET_DATA_PATH = Path("tickets.json")


class TicketStore:
    def __init__(self, path: Path | None = None):
        self.path = path or TICKET_DATA_PATH
        self._data = read_json(self.path, {})

    def _save(self) -> None:
        write_json(self.path, self._data)

    def _guild_key(self, guild_id: int) -> str:
        return str(guild_id)

    def _ensure_guild(self, guild_id: int) -> dict[str, Any]:
        key = self._guild_key(guild_id)
        if key not in self._data:
            self._data[key] = {
                "counter": 0,
                "support_category_id": None,
                "open_by_user": {},
                "tickets": {},
            }
        return self._data[key]

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value in (None, "", False):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def next_ticket_number(self, guild_id: int) -> int:
        guild_state = self._ensure_guild(guild_id)
        guild_state["counter"] = int(guild_state.get("counter", 0)) + 1
        self._save()
        return guild_state["counter"]

    def get_support_category_id(self, guild_id: int) -> int | None:
        guild_state = self._ensure_guild(guild_id)
        return self._coerce_int(guild_state.get("support_category_id"))

    def set_support_category_id(self, guild_id: int, category_id: int | None) -> int | None:
        guild_state = self._ensure_guild(guild_id)
        guild_state["support_category_id"] = self._coerce_int(category_id)
        self._save()
        return self._coerce_int(guild_state.get("support_category_id"))

    def get_open_ticket_channel(self, guild_id: int, user_id: int) -> int | None:
        guild_state = self._ensure_guild(guild_id)
        return self._coerce_int(guild_state.get("open_by_user", {}).get(str(user_id)))

    def clear_open_ticket(self, guild_id: int, user_id: int) -> None:
        guild_state = self._ensure_guild(guild_id)
        guild_state.setdefault("open_by_user", {}).pop(str(user_id), None)
        self._save()

    def register_ticket(
        self,
        guild_id: int,
        *,
        channel_id: int,
        requester_id: int,
        issue_type: str,
        description: str,
    ) -> dict[str, Any]:
        guild_state = self._ensure_guild(guild_id)
        record = {
            "channel_id": int(channel_id),
            "requester_id": int(requester_id),
            "issue_type": str(issue_type),
            "description": str(description),
            "status": "open",
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "closed_at": None,
        }
        guild_state.setdefault("tickets", {})[str(channel_id)] = record
        guild_state.setdefault("open_by_user", {})[str(requester_id)] = int(channel_id)
        self._save()
        return record

    def get_ticket(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        guild_state = self._ensure_guild(guild_id)
        ticket = guild_state.setdefault("tickets", {}).get(str(channel_id))
        return dict(ticket) if isinstance(ticket, dict) else None

    def close_ticket(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        guild_state = self._ensure_guild(guild_id)
        ticket = guild_state.setdefault("tickets", {}).get(str(channel_id))
        if not isinstance(ticket, dict):
            return None

        ticket["status"] = "closed"
        ticket["closed_at"] = datetime.now(timezone.utc).isoformat()
        requester_id = self._coerce_int(ticket.get("requester_id"))
        if requester_id is not None:
            guild_state.setdefault("open_by_user", {}).pop(str(requester_id), None)
        self._save()
        return dict(ticket)
