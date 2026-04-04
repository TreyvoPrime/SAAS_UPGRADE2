from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import threading
from typing import Any

from core.storage import read_json, write_json


TEMP_ROLE_DATA_PATH = Path("dashboard_data/temp_roles.json")


def utc_iso_now() -> str:
    return datetime.now(UTC).isoformat()


class TempRoleStore:
    def __init__(self, path: Path | None = None):
        self.path = path or TEMP_ROLE_DATA_PATH
        self._default = {"items": []}
        self._lock = threading.RLock()
        self._data = read_json(self.path, self._default)

    def _save(self) -> None:
        write_json(self.path, self._data)

    def _refresh(self) -> None:
        self._data = read_json(self.path, self._default)

    def add_assignment(self, guild_id: int, user_id: int, role_id: int, *, expires_at: str, assigned_by_id: int) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            item = {
                "guild_id": int(guild_id),
                "user_id": int(user_id),
                "role_id": int(role_id),
                "expires_at": str(expires_at),
                "assigned_by_id": int(assigned_by_id),
                "created_at": utc_iso_now(),
            }
            items = self._data.setdefault("items", [])
            items = [
                existing for existing in items
                if not (
                    int(existing.get("guild_id", 0)) == int(guild_id)
                    and int(existing.get("user_id", 0)) == int(user_id)
                    and int(existing.get("role_id", 0)) == int(role_id)
                )
            ]
            items.append(item)
            self._data["items"] = items
            self._save()
            return item

    def list_items(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh()
            return list(self._data.setdefault("items", []))

    def remove_assignment(self, guild_id: int, user_id: int, role_id: int) -> bool:
        with self._lock:
            self._refresh()
            items = self._data.setdefault("items", [])
            remaining = [
                item for item in items
                if not (
                    int(item.get("guild_id", 0)) == int(guild_id)
                    and int(item.get("user_id", 0)) == int(user_id)
                    and int(item.get("role_id", 0)) == int(role_id)
                )
            ]
            removed = len(remaining) != len(items)
            self._data["items"] = remaining
            if removed:
                self._save()
            return removed
