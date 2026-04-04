from __future__ import annotations

from pathlib import Path
import threading
from typing import Any

from core.storage import read_json, write_json


SELFROLE_DATA_PATH = Path("dashboard_data/self_roles.json")


class SelfRoleStore:
    def __init__(self, path: Path | None = None):
        self.path = path or SELFROLE_DATA_PATH
        self._default = {"guilds": {}}
        self._lock = threading.RLock()
        self._data = read_json(self.path, self._default)

    def _save(self) -> None:
        write_json(self.path, self._data)

    def _refresh(self) -> None:
        self._data = read_json(self.path, self._default)

    def _guild_bucket(self, guild_id: int) -> dict[str, Any]:
        guilds = self._data.setdefault("guilds", {})
        return guilds.setdefault(str(guild_id), {"role_ids": []})

    def list_roles(self, guild_id: int) -> list[int]:
        with self._lock:
            self._refresh()
            bucket = self._guild_bucket(guild_id)
            seen: set[int] = set()
            role_ids: list[int] = []
            for role_id in bucket.get("role_ids", []):
                try:
                    numeric = int(role_id)
                except (TypeError, ValueError):
                    continue
                if numeric in seen:
                    continue
                seen.add(numeric)
                role_ids.append(numeric)
            return role_ids

    def set_roles(self, guild_id: int, role_ids: list[int]) -> list[int]:
        with self._lock:
            self._refresh()
            bucket = self._guild_bucket(guild_id)
            bucket["role_ids"] = self.list_roles_from_values(role_ids)
            self._save()
        return self.list_roles(guild_id)

    def add_role(self, guild_id: int, role_id: int) -> list[int]:
        current = self.list_roles(guild_id)
        current.append(int(role_id))
        return self.set_roles(guild_id, current)

    def remove_role(self, guild_id: int, role_id: int) -> list[int]:
        remaining = [item for item in self.list_roles(guild_id) if int(item) != int(role_id)]
        return self.set_roles(guild_id, remaining)

    @staticmethod
    def list_roles_from_values(values: list[int] | list[str]) -> list[int]:
        seen: set[int] = set()
        normalized: list[int] = []
        for value in values:
            try:
                numeric = int(value)
            except (TypeError, ValueError):
                continue
            if numeric in seen:
                continue
            seen.add(numeric)
            normalized.append(numeric)
        return normalized
