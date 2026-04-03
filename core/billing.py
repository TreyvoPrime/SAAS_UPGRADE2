from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.storage import read_json, write_json


TIER_FREE = "free"
TIER_PREMIUM = "premium"
PREMIUM_PRICE_DISPLAY = "$2.99/month"

FREE_PREMIUM_COMPARISON_ROWS = [
    {"label": "Core moderation", "free": True, "premium": True},
    {"label": "Core ServerGuard", "free": True, "premium": True},
    {"label": "Dashboard management", "free": True, "premium": True},
    {"label": "Welcome / Leave", "free": True, "premium": True},
    {"label": "Basic support tickets", "free": True, "premium": True},
    {"label": "Standard logging", "free": True, "premium": True},
    {"label": "Basic moderation case history", "free": True, "premium": True},
    {"label": "Standard giveaways", "free": True, "premium": True},
    {"label": "Standard autofeed", "free": True, "premium": True},
    {"label": "Standard reminders", "free": True, "premium": True},
    {"label": "Guardian", "free": False, "premium": True},
    {"label": "Advanced ticket tools", "free": False, "premium": True},
    {"label": "Advanced logs", "free": False, "premium": True},
    {"label": "Advanced moderation history", "free": False, "premium": True},
    {"label": "Advanced giveaway controls", "free": False, "premium": True},
    {"label": "Advanced autofeed controls", "free": False, "premium": True},
    {"label": "Advanced automation", "free": False, "premium": True},
    {"label": "Analytics", "free": False, "premium": True},
    {"label": "Config export / import / presets", "free": False, "premium": True},
]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def _clean_snowflake(value: int | str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_datetime(value: datetime | int | float | str | None) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).astimezone(UTC)
        except ValueError:
            return None
    return None


class BillingStore:
    def __init__(self, path: str | Path = "dashboard_data/billing.json"):
        self.path = Path(path)
        self.data = read_json(
            self.path,
            {
                "guilds": {},
            },
        )

    def save(self) -> None:
        write_json(self.path, self.data)

    def application_id(self) -> int | None:
        return _clean_snowflake(os.getenv("DISCORD_APP_ID") or os.getenv("DISCORD_CLIENT_ID"))

    def premium_sku_id(self) -> int | None:
        return _clean_snowflake(os.getenv("DISCORD_PREMIUM_SKU_ID"))

    def store_url(self) -> str | None:
        explicit = str(os.getenv("DISCORD_PREMIUM_STORE_URL") or "").strip()
        if explicit:
            return explicit
        application_id = self.application_id()
        sku_id = self.premium_sku_id()
        if application_id and sku_id:
            return f"https://discord.com/application-directory/{application_id}/store/{sku_id}"
        return None

    def billing_ready(self) -> bool:
        return bool(self.application_id() and self.premium_sku_id() and self.store_url())

    def _guild_bucket(self, guild_id: int) -> dict[str, Any]:
        guilds = self.data.setdefault("guilds", {})
        return guilds.setdefault(
            str(guild_id),
            {
                "entitlement_id": None,
                "premium_user_id": None,
                "sku_id": None,
                "starts_at": None,
                "ends_at": None,
                "deleted": False,
                "updated_at": None,
            },
        )

    def stored_guild_ids(self) -> list[int]:
        return sorted(int(guild_id) for guild_id in self.data.get("guilds", {}) if str(guild_id).isdigit())

    def upsert_guild_entitlement(
        self,
        guild_id: int | str,
        *,
        entitlement_id: int | str | None = None,
        premium_user_id: int | str | None = None,
        sku_id: int | str | None = None,
        starts_at: datetime | int | float | str | None = None,
        ends_at: datetime | int | float | str | None = None,
        deleted: bool | None = None,
    ) -> dict[str, Any]:
        normalized_guild_id = _clean_snowflake(guild_id)
        if normalized_guild_id is None:
            raise ValueError("A valid guild ID is required.")

        bucket = self._guild_bucket(normalized_guild_id)
        if entitlement_id is not None:
            bucket["entitlement_id"] = str(entitlement_id).strip() or None
        if premium_user_id is not None:
            bucket["premium_user_id"] = _clean_snowflake(premium_user_id)
        if sku_id is not None:
            bucket["sku_id"] = _clean_snowflake(sku_id)
        if starts_at is not None:
            bucket["starts_at"] = _to_iso(_parse_datetime(starts_at))
        if ends_at is not None:
            bucket["ends_at"] = _to_iso(_parse_datetime(ends_at))
        if deleted is not None:
            bucket["deleted"] = bool(deleted)
        bucket["updated_at"] = _to_iso(_utcnow())
        self.save()
        return self.get_guild_assignment(normalized_guild_id)

    def sync_from_entitlement(self, entitlement) -> dict[str, Any] | None:
        guild_id = _clean_snowflake(getattr(entitlement, "guild_id", None))
        if guild_id is None:
            return None
        return self.upsert_guild_entitlement(
            guild_id,
            entitlement_id=getattr(entitlement, "id", None),
            premium_user_id=getattr(entitlement, "user_id", None),
            sku_id=getattr(entitlement, "sku_id", None),
            starts_at=getattr(entitlement, "starts_at", None),
            ends_at=getattr(entitlement, "ends_at", None),
            deleted=bool(getattr(entitlement, "deleted", False)),
        )

    def clear_guild_entitlement(self, guild_id: int | str) -> dict[str, Any]:
        normalized_guild_id = _clean_snowflake(guild_id)
        if normalized_guild_id is None:
            raise ValueError("A valid guild ID is required.")

        bucket = self._guild_bucket(normalized_guild_id)
        bucket["entitlement_id"] = None
        bucket["premium_user_id"] = None
        bucket["sku_id"] = self.premium_sku_id()
        bucket["starts_at"] = None
        bucket["ends_at"] = None
        bucket["deleted"] = False
        bucket["updated_at"] = _to_iso(_utcnow())
        self.save()
        return self.get_guild_assignment(normalized_guild_id)

    def get_guild_assignment(self, guild_id: int | str | None) -> dict[str, Any]:
        normalized_guild_id = _clean_snowflake(guild_id)
        if normalized_guild_id is None:
            return {
                "guild_id": None,
                "premium_user_id": None,
                "entitlement_id": None,
                "sku_id": self.premium_sku_id(),
                "starts_at": None,
                "ends_at": None,
                "assigned_at": None,
                "is_active": False,
            }

        bucket = self._guild_bucket(normalized_guild_id)
        ends_at = _from_iso(bucket.get("ends_at"))
        starts_at = _from_iso(bucket.get("starts_at"))
        assigned_at = _from_iso(bucket.get("updated_at"))
        deleted = bool(bucket.get("deleted", False))
        is_active = bool(bucket.get("entitlement_id")) and not deleted
        if is_active and ends_at is not None and _utcnow() >= ends_at:
            is_active = False

        return {
            "guild_id": normalized_guild_id,
            "premium_user_id": _clean_snowflake(bucket.get("premium_user_id")),
            "entitlement_id": str(bucket.get("entitlement_id") or "").strip() or None,
            "sku_id": _clean_snowflake(bucket.get("sku_id")) or self.premium_sku_id(),
            "starts_at": _to_iso(starts_at),
            "ends_at": _to_iso(ends_at),
            "assigned_at": _to_iso(assigned_at),
            "is_active": is_active,
        }

    def guild_has_active_premium(self, guild_id: int | str) -> bool:
        return bool(self.get_guild_assignment(guild_id)["is_active"])

    def active_guild_count(self) -> int:
        return sum(1 for guild_id in self.stored_guild_ids() if self.guild_has_active_premium(guild_id))

