from __future__ import annotations

from pathlib import Path
from typing import Any

from core.storage import read_json, write_json


class CommandControlStore:
    def __init__(self, path: str | Path = "dashboard_data/command_controls.json"):
        self.path = Path(path)
        self.data = read_json(self.path, {"guilds": {}})

    def save(self) -> None:
        write_json(self.path, self.data)

    def _guild_bucket(self, guild_id: int) -> dict[str, Any]:
        guild_key = str(guild_id)
        guilds = self.data.setdefault("guilds", {})
        return guilds.setdefault(guild_key, {"commands": {}})

    def _command_bucket(self, guild_id: int, command_name: str) -> dict[str, Any]:
        commands = self._guild_bucket(guild_id).setdefault("commands", {})
        return commands.setdefault(
            command_name,
            {"enabled": True, "allowed_role_ids": []},
        )

    def get_policy(self, guild_id: int, command_name: str) -> dict[str, Any]:
        policy = self._command_bucket(guild_id, command_name)
        return {
            "enabled": bool(policy.get("enabled", True)),
            "allowed_role_ids": sorted(
                {
                    int(role_id)
                    for role_id in policy.get("allowed_role_ids", [])
                    if str(role_id).isdigit()
                }
            ),
        }

    def set_enabled(self, guild_id: int, command_name: str, enabled: bool) -> dict[str, Any]:
        bucket = self._command_bucket(guild_id, command_name)
        bucket["enabled"] = bool(enabled)
        self.save()
        return self.get_policy(guild_id, command_name)

    def set_roles(self, guild_id: int, command_name: str, role_ids: list[int]) -> dict[str, Any]:
        bucket = self._command_bucket(guild_id, command_name)
        bucket["allowed_role_ids"] = sorted({int(role_id) for role_id in role_ids})
        self.save()
        return self.get_policy(guild_id, command_name)

