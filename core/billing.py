from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.storage import read_json, write_json


TIER_FREE = "free"
TIER_PREMIUM = "premium"
PREMIUM_PRICE_DISPLAY = "$3.99/month"
ACTIVE_SUBSCRIPTION_STATUSES = {"active", "trialing", "past_due"}
INACTIVE_SUBSCRIPTION_STATUSES = {"canceled", "unpaid", "incomplete", "incomplete_expired"}

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


def _timestamp_to_iso(value: int | float | str | None) -> str | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(int(value), UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _clean_user_id(value: int | str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def normalize_subscription_status(value: str | None) -> str:
    status = str(value or "").strip().lower()
    if not status:
        return "inactive"
    return status


def subscription_status_label(value: str | None) -> str:
    status = normalize_subscription_status(value)
    if status == "inactive":
        return "Free"
    return status.replace("_", " ").title()


class BillingStore:
    def __init__(self, path: str | Path = "dashboard_data/billing.json"):
        self.path = Path(path)
        self.data = read_json(
            self.path,
            {
                "users": {},
                "guilds": {},
                "processed_webhook_ids": [],
            },
        )

    def save(self) -> None:
        write_json(self.path, self.data)

    def billing_ready(self) -> bool:
        return bool(
            os.getenv("STRIPE_SECRET_KEY")
            and os.getenv("STRIPE_PREMIUM_PRICE_ID")
            and os.getenv("STRIPE_WEBHOOK_SECRET")
        )

    def _user_bucket(self, user_id: int) -> dict[str, Any]:
        users = self.data.setdefault("users", {})
        return users.setdefault(
            str(user_id),
            {
                "stripe_customer_id": None,
                "stripe_subscription_id": None,
                "subscription_status": "inactive",
                "current_period_end": None,
                "cancel_at_period_end": False,
                "email": None,
                "last_checkout_session_id": None,
                "updated_at": None,
            },
        )

    def _guild_bucket(self, guild_id: int) -> dict[str, Any]:
        guilds = self.data.setdefault("guilds", {})
        return guilds.setdefault(
            str(guild_id),
            {
                "premium_user_id": None,
                "assigned_at": None,
            },
        )

    def get_user_subscription(self, user_id: int | str | None) -> dict[str, Any]:
        normalized_user_id = _clean_user_id(user_id)
        if normalized_user_id is None:
            return {
                "user_id": None,
                "stripe_customer_id": None,
                "stripe_subscription_id": None,
                "status": "inactive",
                "status_label": "Free",
                "is_active": False,
                "current_period_end": None,
                "cancel_at_period_end": False,
                "email": None,
                "last_checkout_session_id": None,
                "active_guild_ids": [],
            }

        bucket = self._user_bucket(normalized_user_id)
        status = normalize_subscription_status(bucket.get("subscription_status"))
        active_guild_ids = [
            int(guild_id)
            for guild_id, guild_bucket in self.data.get("guilds", {}).items()
            if str(guild_id).isdigit() and int(guild_bucket.get("premium_user_id") or 0) == normalized_user_id
        ]
        current_period_end = _from_iso(bucket.get("current_period_end"))
        return {
            "user_id": normalized_user_id,
            "stripe_customer_id": bucket.get("stripe_customer_id"),
            "stripe_subscription_id": bucket.get("stripe_subscription_id"),
            "status": status,
            "status_label": subscription_status_label(status),
            "is_active": status in ACTIVE_SUBSCRIPTION_STATUSES,
            "current_period_end": _to_iso(current_period_end),
            "cancel_at_period_end": bool(bucket.get("cancel_at_period_end", False)),
            "email": bucket.get("email"),
            "last_checkout_session_id": bucket.get("last_checkout_session_id"),
            "active_guild_ids": sorted(active_guild_ids),
        }

    def user_has_active_premium(self, user_id: int | str | None) -> bool:
        return bool(self.get_user_subscription(user_id)["is_active"])

    def set_checkout_session(self, user_id: int | str, session_id: str | None) -> dict[str, Any]:
        normalized_user_id = _clean_user_id(user_id)
        if normalized_user_id is None:
            raise ValueError("A valid Discord user ID is required.")
        bucket = self._user_bucket(normalized_user_id)
        bucket["last_checkout_session_id"] = str(session_id or "").strip() or None
        bucket["updated_at"] = _to_iso(_utcnow())
        self.save()
        return self.get_user_subscription(normalized_user_id)

    def upsert_subscription(
        self,
        user_id: int | str,
        *,
        stripe_customer_id: str | None = None,
        stripe_subscription_id: str | None = None,
        status: str | None = None,
        current_period_end: int | float | str | None = None,
        cancel_at_period_end: bool | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        normalized_user_id = _clean_user_id(user_id)
        if normalized_user_id is None:
            raise ValueError("A valid Discord user ID is required.")

        bucket = self._user_bucket(normalized_user_id)
        if stripe_customer_id is not None:
            bucket["stripe_customer_id"] = str(stripe_customer_id).strip() or None
        if stripe_subscription_id is not None:
            bucket["stripe_subscription_id"] = str(stripe_subscription_id).strip() or None
        if status is not None:
            bucket["subscription_status"] = normalize_subscription_status(status)
        if current_period_end is not None:
            bucket["current_period_end"] = _timestamp_to_iso(current_period_end) or (
                str(current_period_end).strip() if isinstance(current_period_end, str) else None
            )
        if cancel_at_period_end is not None:
            bucket["cancel_at_period_end"] = bool(cancel_at_period_end)
        if email is not None:
            bucket["email"] = str(email).strip() or None
        bucket["updated_at"] = _to_iso(_utcnow())
        self.save()
        return self.get_user_subscription(normalized_user_id)

    def clear_subscription(self, user_id: int | str, *, preserve_customer: bool = True) -> dict[str, Any]:
        normalized_user_id = _clean_user_id(user_id)
        if normalized_user_id is None:
            raise ValueError("A valid Discord user ID is required.")

        bucket = self._user_bucket(normalized_user_id)
        if not preserve_customer:
            bucket["stripe_customer_id"] = None
        bucket["stripe_subscription_id"] = None
        bucket["subscription_status"] = "inactive"
        bucket["current_period_end"] = None
        bucket["cancel_at_period_end"] = False
        bucket["updated_at"] = _to_iso(_utcnow())
        self.save()
        return self.get_user_subscription(normalized_user_id)

    def find_user_id_by_customer_id(self, customer_id: str | None) -> int | None:
        if not customer_id:
            return None
        for user_id, bucket in self.data.get("users", {}).items():
            if str(bucket.get("stripe_customer_id") or "") == str(customer_id):
                return int(user_id)
        return None

    def find_user_id_by_subscription_id(self, subscription_id: str | None) -> int | None:
        if not subscription_id:
            return None
        for user_id, bucket in self.data.get("users", {}).items():
            if str(bucket.get("stripe_subscription_id") or "") == str(subscription_id):
                return int(user_id)
        return None

    def assign_guild(self, guild_id: int | str, user_id: int | str) -> dict[str, Any]:
        normalized_guild_id = _clean_user_id(guild_id)
        normalized_user_id = _clean_user_id(user_id)
        if normalized_guild_id is None or normalized_user_id is None:
            raise ValueError("A valid guild ID and user ID are required.")

        bucket = self._guild_bucket(normalized_guild_id)
        bucket["premium_user_id"] = normalized_user_id
        bucket["assigned_at"] = _to_iso(_utcnow())
        self.save()
        return self.get_guild_assignment(normalized_guild_id)

    def unassign_guild(self, guild_id: int | str) -> dict[str, Any]:
        normalized_guild_id = _clean_user_id(guild_id)
        if normalized_guild_id is None:
            raise ValueError("A valid guild ID is required.")

        bucket = self._guild_bucket(normalized_guild_id)
        bucket["premium_user_id"] = None
        bucket["assigned_at"] = None
        self.save()
        return self.get_guild_assignment(normalized_guild_id)

    def get_guild_assignment(self, guild_id: int | str) -> dict[str, Any]:
        normalized_guild_id = _clean_user_id(guild_id)
        if normalized_guild_id is None:
            return {
                "guild_id": None,
                "premium_user_id": None,
                "assigned_at": None,
                "is_active": False,
            }

        bucket = self._guild_bucket(normalized_guild_id)
        premium_user_id = _clean_user_id(bucket.get("premium_user_id"))
        assigned_at = _from_iso(bucket.get("assigned_at"))
        return {
            "guild_id": normalized_guild_id,
            "premium_user_id": premium_user_id,
            "assigned_at": _to_iso(assigned_at),
            "is_active": self.user_has_active_premium(premium_user_id),
        }

    def guild_has_active_premium(self, guild_id: int | str) -> bool:
        assignment = self.get_guild_assignment(guild_id)
        return bool(assignment["premium_user_id"] and assignment["is_active"])

    def processed_webhook_ids(self) -> list[str]:
        values = self.data.setdefault("processed_webhook_ids", [])
        return [str(value) for value in values if str(value).strip()]

    def has_processed_webhook(self, event_id: str | None) -> bool:
        if not event_id:
            return False
        return str(event_id) in self.processed_webhook_ids()

    def mark_webhook_processed(self, event_id: str | None) -> None:
        if not event_id:
            return
        items = self.processed_webhook_ids()
        event_key = str(event_id)
        if event_key in items:
            return
        items.insert(0, event_key)
        self.data["processed_webhook_ids"] = items[:200]
        self.save()
