from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage import read_json, write_json


TICKET_DATA_PATH = Path("tickets.json")
DEFAULT_ISSUE_TYPES = [
    "Moderation Help",
    "Server Setup Help",
    "Role or Channel Issue",
    "Report a Member",
    "Appeal or Review",
    "Other",
]


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
                "issue_types": list(DEFAULT_ISSUE_TYPES),
                "open_by_user": {},
                "tickets": {},
            }
        else:
            guild_state = self._data[key]
            if not isinstance(guild_state.get("issue_types"), list) or not guild_state.get("issue_types"):
                guild_state["issue_types"] = list(DEFAULT_ISSUE_TYPES)
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

    def get_issue_types(self, guild_id: int) -> list[str]:
        guild_state = self._ensure_guild(guild_id)
        raw_items = guild_state.get("issue_types", DEFAULT_ISSUE_TYPES)
        cleaned = [
            str(item).strip()
            for item in raw_items
            if str(item).strip()
        ]
        if not cleaned:
            cleaned = list(DEFAULT_ISSUE_TYPES)
            guild_state["issue_types"] = cleaned
            self._save()
        return cleaned

    def set_issue_types(self, guild_id: int, issue_types: list[str]) -> list[str]:
        guild_state = self._ensure_guild(guild_id)
        normalized: list[str] = []
        seen: set[str] = set()
        for item in issue_types:
            label = str(item).strip()
            if not label:
                continue
            key = label.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(label[:80])
        guild_state["issue_types"] = normalized[:20] or list(DEFAULT_ISSUE_TYPES)
        self._save()
        return self.get_issue_types(guild_id)

    def add_issue_type(self, guild_id: int, issue_type: str) -> list[str]:
        current = self.get_issue_types(guild_id)
        current.append(issue_type)
        return self.set_issue_types(guild_id, current)

    def remove_issue_type(self, guild_id: int, issue_type: str) -> list[str]:
        target = str(issue_type).strip().casefold()
        remaining = [item for item in self.get_issue_types(guild_id) if item.casefold() != target]
        return self.set_issue_types(guild_id, remaining)

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
            "ticket_number": int(guild_state.get("counter", 0)),
            "requester_id": int(requester_id),
            "issue_type": str(issue_type),
            "description": str(description),
            "status": "open",
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "closed_at": None,
            "claimed_by_id": None,
            "claimed_by_name": None,
            "priority": "normal",
            "close_reason": None,
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
        return self.close_ticket_with_reason(guild_id, channel_id, reason=None)

    def close_ticket_with_reason(self, guild_id: int, channel_id: int, *, reason: str | None) -> dict[str, Any] | None:
        guild_state = self._ensure_guild(guild_id)
        ticket = guild_state.setdefault("tickets", {}).get(str(channel_id))
        if not isinstance(ticket, dict):
            return None

        ticket["status"] = "closed"
        ticket["closed_at"] = datetime.now(timezone.utc).isoformat()
        ticket["close_reason"] = str(reason).strip() if reason else None
        requester_id = self._coerce_int(ticket.get("requester_id"))
        if requester_id is not None:
            guild_state.setdefault("open_by_user", {}).pop(str(requester_id), None)
        self._save()
        return dict(ticket)

    def claim_ticket(self, guild_id: int, channel_id: int, *, staff_id: int, staff_name: str) -> dict[str, Any] | None:
        guild_state = self._ensure_guild(guild_id)
        ticket = guild_state.setdefault("tickets", {}).get(str(channel_id))
        if not isinstance(ticket, dict):
            return None
        ticket["claimed_by_id"] = int(staff_id)
        ticket["claimed_by_name"] = str(staff_name)
        self._save()
        return dict(ticket)

    def set_priority(self, guild_id: int, channel_id: int, priority: str) -> dict[str, Any] | None:
        guild_state = self._ensure_guild(guild_id)
        ticket = guild_state.setdefault("tickets", {}).get(str(channel_id))
        if not isinstance(ticket, dict):
            return None
        ticket["priority"] = str(priority).strip().lower()
        self._save()
        return dict(ticket)

    def list_tickets(self, guild_id: int, *, status: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        guild_state = self._ensure_guild(guild_id)
        tickets = [
            dict(ticket)
            for ticket in guild_state.setdefault("tickets", {}).values()
            if isinstance(ticket, dict) and (status is None or ticket.get("status") == status)
        ]
        tickets.sort(key=lambda item: item.get("opened_at", ""), reverse=True)
        return tickets[:limit]
