from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import threading
from typing import Any

from core.premium import TIER_FREE, TIER_PREMIUM, normalize_tier
from core.storage import read_json, write_json


class CommandControlStore:
    DEFAULT_SUBSCRIPTION_TIER = TIER_FREE
    DEFAULT_PURGE_LIMIT = 100
    FREE_PURGE_LIMIT_CAP = 500
    PREMIUM_PURGE_LIMIT_CAP = 2000
    DEFAULT_PURGE_MODE = "all"
    VALID_PURGE_MODES = {"all", "bots", "humans", "links", "attachments", "embeds", "mentions"}
    DEFAULT_TIMEOUT_MINUTES = 10
    MIN_TIMEOUT_MINUTES = 1
    MAX_TIMEOUT_MINUTES = 40320
    DEFAULT_ALERT_CONFIRMATION_ENABLED = True
    DEFAULT_ALERT_SKIP_IN_VOICE = True
    DEFAULT_ALERT_ONLY_OFFLINE = False
    DEFAULT_ALERT_INCLUDE_BOTS = False
    DEFAULT_ALERT_COOLDOWN_SECONDS = 120
    MIN_ALERT_COOLDOWN_SECONDS = 15
    MAX_ALERT_COOLDOWN_SECONDS = 900

    def __init__(self, path: str | Path = "dashboard_data/command_controls.json"):
        self.path = Path(path)
        self._default = {"guilds": {}}
        self._lock = threading.RLock()
        self.data = read_json(self.path, self._default)
        self.billing_store = None

    def attach_billing_store(self, billing_store) -> None:
        self.billing_store = billing_store

    def save(self) -> None:
        write_json(self.path, self.data)

    def _refresh(self) -> None:
        self.data = read_json(self.path, self._default)

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
        with self._lock:
            self._refresh()
            policy = self._command_bucket(guild_id, command_name)
        return {
            "enabled": bool(policy.get("enabled", True)),
            "restrict_to_roles": bool(policy.get("restrict_to_roles", False)),
            "allowed_role_ids": sorted(
                {
                    int(role_id)
                    for role_id in policy.get("allowed_role_ids", [])
                    if str(role_id).isdigit()
                }
            ),
        }

    def set_enabled(self, guild_id: int, command_name: str, enabled: bool) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            bucket = self._command_bucket(guild_id, command_name)
            bucket["enabled"] = bool(enabled)
            self.save()
        return self.get_policy(guild_id, command_name)

    def set_roles(
        self,
        guild_id: int,
        command_name: str,
        role_ids: list[int],
        *,
        restrict_to_roles: bool | None = None,
    ) -> dict[str, Any]:
        return self.set_roles_for_commands(
            guild_id,
            [command_name],
            role_ids,
            restrict_to_roles=restrict_to_roles,
        )[command_name]

    def set_roles_for_commands(
        self,
        guild_id: int,
        command_names: list[str],
        role_ids: list[int],
        *,
        restrict_to_roles: bool | None = None,
    ) -> dict[str, dict[str, Any]]:
        normalized_roles = sorted({int(role_id) for role_id in role_ids})
        unique_commands = list(dict.fromkeys(str(name) for name in command_names if str(name).strip()))
        with self._lock:
            self._refresh()
            for command_name in unique_commands:
                bucket = self._command_bucket(guild_id, command_name)
                bucket["allowed_role_ids"] = normalized_roles
                if restrict_to_roles is None:
                    bucket["restrict_to_roles"] = bool(bucket["allowed_role_ids"])
                else:
                    bucket["restrict_to_roles"] = bool(restrict_to_roles)
            self.save()
        return {command_name: self.get_policy(guild_id, command_name) for command_name in unique_commands}

    def get_dashboard_editor_roles(self, guild_id: int) -> list[int]:
        with self._lock:
            self._refresh()
            dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
            return sorted(
                {
                    int(role_id)
                    for role_id in dashboard_bucket.get("editor_role_ids", [])
                    if str(role_id).isdigit()
                }
            )

    def set_dashboard_editor_roles(self, guild_id: int, role_ids: list[int]) -> list[int]:
        with self._lock:
            self._refresh()
            dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
            dashboard_bucket["editor_role_ids"] = sorted({int(role_id) for role_id in role_ids})
            self.save()
        return self.get_dashboard_editor_roles(guild_id)

    def get_subscription_tier(self, guild_id: int) -> str:
        with self._lock:
            self._refresh()
            dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
            stored_tier = normalize_tier(dashboard_bucket.get("subscription_tier", self.DEFAULT_SUBSCRIPTION_TIER))
        billing_store = getattr(self, "billing_store", None)
        if billing_store is not None and hasattr(billing_store, "billing_ready") and billing_store.billing_ready():
            return TIER_PREMIUM if billing_store.guild_has_active_premium(guild_id) else TIER_FREE
        return stored_tier

    def set_subscription_tier(self, guild_id: int, tier: str) -> str:
        with self._lock:
            self._refresh()
            dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
            normalized = normalize_tier(tier)
            dashboard_bucket["subscription_tier"] = normalized
            if normalized != TIER_PREMIUM:
                raw_value = dashboard_bucket.get("purge_limit", self.DEFAULT_PURGE_LIMIT)
                try:
                    current_limit = int(raw_value)
                except (TypeError, ValueError):
                    current_limit = self.DEFAULT_PURGE_LIMIT
                dashboard_bucket["purge_limit"] = min(current_limit, self.FREE_PURGE_LIMIT_CAP)
            self.save()
        return self.get_subscription_tier(guild_id)

    def is_premium_enabled(self, guild_id: int) -> bool:
        return self.get_subscription_tier(guild_id) == TIER_PREMIUM

    def list_config_templates(self, guild_id: int) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh()
            dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
            templates = dashboard_bucket.get("config_templates", [])
            if not isinstance(templates, list):
                return []
            cleaned: list[dict[str, Any]] = []
            for item in templates:
                if not isinstance(item, dict):
                    continue
                cleaned.append(
                    {
                        "name": str(item.get("name") or "Untitled")[:60],
                        "created_at": str(item.get("created_at") or ""),
                        "snapshot": item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {},
                    }
                )
        return cleaned[:8]

    def save_config_template(self, guild_id: int, name: str, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh()
            dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
            raw_templates = dashboard_bucket.get("config_templates", [])
            cleaned: list[dict[str, Any]] = []
            if isinstance(raw_templates, list):
                for item in raw_templates:
                    if not isinstance(item, dict):
                        continue
                    cleaned.append(
                        {
                            "name": str(item.get("name") or "Untitled")[:60],
                            "created_at": str(item.get("created_at") or ""),
                            "snapshot": item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {},
                        }
                    )
            templates = [item for item in cleaned[:8] if item["name"].casefold() != str(name).strip().casefold()]
            templates.insert(
                0,
                {
                    "name": str(name).strip()[:60] or "Untitled",
                    "created_at": datetime.now(UTC).isoformat(),
                    "snapshot": snapshot if isinstance(snapshot, dict) else {},
                },
            )
            dashboard_bucket["config_templates"] = templates[:8]
            self.save()
        return self.list_config_templates(guild_id)

    def get_autorole_role_ids(self, guild_id: int) -> list[int]:
        with self._lock:
            self._refresh()
            dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
            return sorted(
                {
                    int(role_id)
                    for role_id in dashboard_bucket.get("autorole_role_ids", [])
                    if str(role_id).isdigit()
                }
            )

    def set_autorole_role_ids(self, guild_id: int, role_ids: list[int]) -> list[int]:
        with self._lock:
            self._refresh()
            dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
            dashboard_bucket["autorole_role_ids"] = sorted({int(role_id) for role_id in role_ids})
            self.save()
        return self.get_autorole_role_ids(guild_id)

    def is_setup_wizard_completed(self, guild_id: int) -> bool:
        with self._lock:
            self._refresh()
            dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
            return bool(dashboard_bucket.get("setup_wizard_completed", False))

    def set_setup_wizard_completed(self, guild_id: int, completed: bool) -> bool:
        with self._lock:
            self._refresh()
            dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
            dashboard_bucket["setup_wizard_completed"] = bool(completed)
            self.save()
        return self.is_setup_wizard_completed(guild_id)

    def get_purge_limit(self, guild_id: int) -> int:
        return self.get_purge_settings(guild_id)["limit"]

    def get_purge_settings(self, guild_id: int) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
            raw_value = dashboard_bucket.get("purge_limit", self.DEFAULT_PURGE_LIMIT)
            try:
                normalized = int(raw_value)
            except (TypeError, ValueError):
                normalized = self.DEFAULT_PURGE_LIMIT
            normalized_limit = max(1, min(normalized, self.PREMIUM_PURGE_LIMIT_CAP))
            mode = str(dashboard_bucket.get("purge_default_mode", self.DEFAULT_PURGE_MODE)).strip().lower()
            if mode not in self.VALID_PURGE_MODES:
                mode = self.DEFAULT_PURGE_MODE
            include_pinned_default = bool(dashboard_bucket.get("purge_include_pinned_default", False))
            return {
                "limit": normalized_limit,
                "default_mode": mode,
                "include_pinned_default": include_pinned_default,
            }

    def set_purge_limit(self, guild_id: int, limit: int) -> int:
        return self.set_purge_settings(guild_id, limit=limit)["limit"]

    def set_purge_settings(
        self,
        guild_id: int,
        *,
        limit: int | None = None,
        default_mode: str | None = None,
        include_pinned_default: bool | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
            if limit is not None:
                dashboard_bucket["purge_limit"] = max(1, min(int(limit), self.PREMIUM_PURGE_LIMIT_CAP))
            if default_mode is not None:
                normalized_mode = str(default_mode).strip().lower()
                dashboard_bucket["purge_default_mode"] = (
                    normalized_mode if normalized_mode in self.VALID_PURGE_MODES else self.DEFAULT_PURGE_MODE
                )
            if include_pinned_default is not None:
                dashboard_bucket["purge_include_pinned_default"] = bool(include_pinned_default)
            self.save()
        return self.get_purge_settings(guild_id)

    def get_moderation_settings(self, guild_id: int) -> dict[str, Any]:
        with self._lock:
            self._refresh()
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
        with self._lock:
            self._refresh()
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

    def get_alert_settings(self, guild_id: int) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
            raw_cooldown = dashboard_bucket.get("alert_cooldown_seconds", self.DEFAULT_ALERT_COOLDOWN_SECONDS)
            try:
                cooldown_seconds = int(raw_cooldown)
            except (TypeError, ValueError):
                cooldown_seconds = self.DEFAULT_ALERT_COOLDOWN_SECONDS
            cooldown_seconds = max(self.MIN_ALERT_COOLDOWN_SECONDS, min(cooldown_seconds, self.MAX_ALERT_COOLDOWN_SECONDS))
            return {
                "confirmation_enabled": bool(
                    dashboard_bucket.get("alert_confirmation_enabled", self.DEFAULT_ALERT_CONFIRMATION_ENABLED)
                ),
                "skip_in_voice_default": bool(
                    dashboard_bucket.get("alert_skip_in_voice_default", self.DEFAULT_ALERT_SKIP_IN_VOICE)
                ),
                "only_offline_default": bool(
                    dashboard_bucket.get("alert_only_offline_default", self.DEFAULT_ALERT_ONLY_OFFLINE)
                ),
                "include_bots_default": bool(
                    dashboard_bucket.get("alert_include_bots_default", self.DEFAULT_ALERT_INCLUDE_BOTS)
                ),
                "cooldown_seconds": cooldown_seconds,
            }

    def set_alert_settings(
        self,
        guild_id: int,
        *,
        confirmation_enabled: bool | None = None,
        skip_in_voice_default: bool | None = None,
        only_offline_default: bool | None = None,
        include_bots_default: bool | None = None,
        cooldown_seconds: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            dashboard_bucket = self._guild_bucket(guild_id).setdefault("dashboard", {"editor_role_ids": []})
            if confirmation_enabled is not None:
                dashboard_bucket["alert_confirmation_enabled"] = bool(confirmation_enabled)
            if skip_in_voice_default is not None:
                dashboard_bucket["alert_skip_in_voice_default"] = bool(skip_in_voice_default)
            if only_offline_default is not None:
                dashboard_bucket["alert_only_offline_default"] = bool(only_offline_default)
            if include_bots_default is not None:
                dashboard_bucket["alert_include_bots_default"] = bool(include_bots_default)
            if cooldown_seconds is not None:
                dashboard_bucket["alert_cooldown_seconds"] = max(
                    self.MIN_ALERT_COOLDOWN_SECONDS,
                    min(int(cooldown_seconds), self.MAX_ALERT_COOLDOWN_SECONDS),
                )
            self.save()
        return self.get_alert_settings(guild_id)
