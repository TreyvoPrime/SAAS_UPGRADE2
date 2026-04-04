from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import threading
from typing import Any

from core.storage import read_json, write_json


def _utc_iso_now() -> str:
    return datetime.now(UTC).isoformat()


class WarningStore:
    def __init__(self, path: str | Path = "dashboard_data/warnings.json"):
        self.path = Path(path)
        self._default = {"guilds": {}}
        self._lock = threading.RLock()
        self.data = read_json(self.path, self._default)

    def save(self) -> None:
        write_json(self.path, self.data)

    def _refresh(self) -> None:
        self.data = read_json(self.path, self._default)

    def _guild_bucket(self, guild_id: int) -> dict[str, Any]:
        guilds = self.data.setdefault("guilds", {})
        return guilds.setdefault(str(guild_id), {"users": {}})

    def add_warning(
        self,
        guild_id: int,
        user_id: int,
        *,
        moderator_id: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            guild_bucket = self._guild_bucket(guild_id)
            users_bucket = guild_bucket.setdefault("users", {})
            warnings = users_bucket.setdefault(str(user_id), [])
            warning = {
                "moderator_id": int(moderator_id),
                "reason": (reason or "No reason provided.").strip(),
                "timestamp": _utc_iso_now(),
            }
            warnings.append(warning)
            self.save()
            return warning

    def list_warnings(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh()
            guild_bucket = self._guild_bucket(guild_id)
            users_bucket = guild_bucket.setdefault("users", {})
            return list(users_bucket.get(str(user_id), []))

    def warning_count(self, guild_id: int, user_id: int) -> int:
        return len(self.list_warnings(guild_id, user_id))
