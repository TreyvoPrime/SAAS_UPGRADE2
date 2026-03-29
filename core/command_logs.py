from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage import read_json, write_json


class CommandLogStore:
    def __init__(self, path: str | Path = "dashboard_data/command_usage_logs.json", limit: int = 2000):
        self.path = Path(path)
        self.limit = limit
        self.data = read_json(self.path, {"entries": []})

    def save(self) -> None:
        write_json(self.path, self.data)

    def append(self, entry: dict[str, Any]) -> None:
        entries = self.data.setdefault("entries", [])
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **entry,
        }
        entries.append(payload)
        if len(entries) > self.limit:
            self.data["entries"] = entries[-self.limit :]
        self.save()

    def list_for_guild(self, guild_id: int, limit: int = 100) -> list[dict[str, Any]]:
        guild_key = str(guild_id)
        entries = [
            entry for entry in self.data.get("entries", [])
            if str(entry.get("guild_id")) == guild_key
        ]
        return list(reversed(entries[-limit:]))

