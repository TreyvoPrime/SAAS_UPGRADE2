from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage import get_storage_backend, read_json, write_json


class CommandLogStore:
    def __init__(self, path: str | Path = "dashboard_data/command_usage_logs.json", limit: int = 2000):
        self.path = Path(path)
        self.limit = limit
        self.data = read_json(self.path, {"entries": []})

    def save(self) -> None:
        if get_storage_backend() is not None:
            return
        write_json(self.path, self.data)

    def append(self, entry: dict[str, Any]) -> None:
        entries = self.data.setdefault("entries", [])
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **entry,
        }
        backend = get_storage_backend()
        if backend is not None:
            backend.append_command_log(payload)
            return
        entries.append(payload)
        if len(entries) > self.limit:
            self.data["entries"] = entries[-self.limit :]
        self.save()

    def list_for_guild(
        self,
        guild_id: int,
        limit: int = 100,
        *,
        query: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        category: str | None = None,
        actor: str | None = None,
    ) -> list[dict[str, Any]]:
        backend = get_storage_backend()
        if backend is not None:
            return backend.list_command_logs(
                guild_id,
                limit=limit,
                query=query,
                kind=kind,
                status=status,
                category=category,
                actor=actor,
            )
        guild_key = str(guild_id)
        entries = [
            entry for entry in self.data.get("entries", [])
            if str(entry.get("guild_id")) == guild_key
        ]
        if query:
            needle = str(query).strip().lower()
            entries = [
                entry for entry in entries
                if needle in str(entry.get("title") or "").lower()
                or needle in str(entry.get("summary") or "").lower()
                or needle in str(entry.get("command") or "").lower()
                or needle in str(entry.get("user_name") or "").lower()
                or needle in str(entry.get("channel_name") or "").lower()
            ]
        if kind:
            entries = [entry for entry in entries if str(entry.get("kind") or "").lower() == str(kind).lower()]
        if status:
            entries = [entry for entry in entries if str(entry.get("status") or "").lower() == str(status).lower()]
        if category:
            entries = [entry for entry in entries if str(entry.get("category") or "").lower() == str(category).lower()]
        if actor:
            needle = str(actor).strip().lower()
            entries = [entry for entry in entries if needle in str(entry.get("user_name") or "").lower()]
        return list(reversed(entries[-limit:]))

    def export_for_guild(
        self,
        guild_id: int,
        *,
        limit: int = 1000,
        query: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        category: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        return {
            "guild_id": int(guild_id),
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "entries": self.list_for_guild(
                guild_id,
                limit=limit,
                query=query,
                kind=kind,
                status=status,
                category=category,
                actor=actor,
            ),
        }

