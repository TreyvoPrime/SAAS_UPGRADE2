from __future__ import annotations

from pathlib import Path
from typing import Any

from core.storage import read_json, write_json


class CommandControlStore:
    DEFAULT_PURGE_LIMIT = 100
    FREE_PURGE_LIMIT_CAP = 500
    PREMIUM_PURGE_LIMIT_CAP = 2000
    DEFAULT_TIMEOUT_MINUTES = 10
    MIN_TIMEOUT_MINUTES = 1
    MAX_TIMEOUT_MINUTES = 40320

    def __init__(self, path: str | Path = "dashboard_data/command_controls.json"):
        self.path = Path(path)
        self.data = read_json(self.path, {"guilds": {}})

    def save(self) -> None:
        write_json(self.path, self.data)

    def _guild_bucket(self, guild_id: int) -> dict[str, Any]:
        guild_key = str(guild_id)
        guilds = self.data.setdefault("guilds", {})
        return guilds.setdefault(guild_key, {"commands": {}, "dashboard": {"editor_role_ids": []}})

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

    def get_dashboard_editor_roles(self, guild_id: int) -> list[int]:
        dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
        return sorted(
            {
                int(role_id)
                for role_id in dashboard_bucket.get("editor_role_ids", [])
                if str(role_id).isdigit()
            }
        )

    def set_dashboard_editor_roles(self, guild_id: int, role_ids: list[int]) -> list[int]:
        dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
        dashboard_bucket["editor_role_ids"] = sorted({int(role_id) for role_id in role_ids})
        self.save()
        return self.get_dashboard_editor_roles(guild_id)

    def get_autorole_role_ids(self, guild_id: int) -> list[int]:
        dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
        return sorted(
            {
                int(role_id)
                for role_id in dashboard_bucket.get("autorole_role_ids", [])
                if str(role_id).isdigit()
            }
        )

    def set_autorole_role_ids(self, guild_id: int, role_ids: list[int]) -> list[int]:
        dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
        dashboard_bucket["autorole_role_ids"] = sorted({int(role_id) for role_id in role_ids})
        self.save()
        return self.get_autorole_role_ids(guild_id)

    def is_setup_wizard_completed(self, guild_id: int) -> bool:
        dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
        return bool(dashboard_bucket.get("setup_wizard_completed", False))

    def set_setup_wizard_completed(self, guild_id: int, completed: bool) -> bool:
        dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
        dashboard_bucket["setup_wizard_completed"] = bool(completed)
        self.save()
        return self.is_setup_wizard_completed(guild_id)

    def get_purge_limit(self, guild_id: int) -> int:
        dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
        raw_value = dashboard_bucket.get("purge_limit", self.DEFAULT_PURGE_LIMIT)
        try:
            normalized = int(raw_value)
        except (TypeError, ValueError):
            normalized = self.DEFAULT_PURGE_LIMIT
        return max(1, min(normalized, self.PREMIUM_PURGE_LIMIT_CAP))

    def set_purge_limit(self, guild_id: int, limit: int) -> int:
        dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
        dashboard_bucket["purge_limit"] = max(1, min(int(limit), self.PREMIUM_PURGE_LIMIT_CAP))
        self.save()
        return self.get_purge_limit(guild_id)

    def get_moderation_settings(self, guild_id: int) -> dict[str, Any]:
        dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
        confirmation_enabled = bool(dashboard_bucket.get("moderation_confirmation_enabled", True))
        raw_timeout = dashboard_bucket.get("default_timeout_minutes", self.DEFAULT_TIMEOUT_MINUTES)
        try:
            timeout_minutes = int(raw_timeout)
        except (TypeError, ValueError):
            timeout_minutes = self.DEFAULT_TIMEOUT_MINUTES
        timeout_minutes = max(self.MIN_TIMEOUT_MINUTES, min(timeout_minutes, self.MAX_TIMEOUT_MINUTES))
        return {
            "confirmation_enabled": confirmation_enabled,
            "default_timeout_minutes": timeout_minutes,
        }

    def set_moderation_settings(
        self,
        guild_id: int,
        *,
        confirmation_enabled: bool | None = None,
        default_timeout_minutes: int | None = None,
    ) -> dict[str, Any]:
        dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
        if confirmation_enabled is not None:
            dashboard_bucket["moderation_confirmation_enabled"] = bool(confirmation_enabled)
        if default_timeout_minutes is not None:
            dashboard_bucket["default_timeout_minutes"] = max(
                self.MIN_TIMEOUT_MINUTES,
                min(int(default_timeout_minutes), self.MAX_TIMEOUT_MINUTES),
            )
        self.save()
        return self.get_moderation_settings(guild_id)
