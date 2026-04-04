from __future__ import annotations

TIER_FREE = "free"
TIER_PREMIUM = "premium"
VALID_TIERS = {TIER_FREE, TIER_PREMIUM}

PREMIUM_FEATURES: dict[str, str] = {
    "guardian": "Guardian threat scoring and automated raid response",
    "advanced_tickets": "Ticket claiming, priority, transcripts, internal notes, and ticket analytics",
    "advanced_moderation": "Case exports, richer history tools, and moderation presets",
    "advanced_logging": "Searchable logs, exports, and deeper log controls",
    "advanced_serverguard": "Guardian presets, blacklist controls, whitelist controls, and exception tuning",
    "advanced_automation": "Recurring reminders, richer autofeed controls, and onboarding presets",
    "analytics": "Growth, moderation, command, ticket, and staff analytics",
    "premium_giveaways": "Role requirements, bonus entries, and advanced giveaway rules",
    "unlimited_limits": "Unlimited custom commands, autoresponders, and reaction role panels",
}


def normalize_tier(value: str | None) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in VALID_TIERS else TIER_FREE


def tier_label(value: str | None) -> str:
    return "Premium" if normalize_tier(value) == TIER_PREMIUM else "Free"


def premium_required_message(feature_name: str) -> str:
    return f"{feature_name} is part of ServerCore Premium right now."


def guild_has_premium(bot, guild_id: int | None, interaction=None) -> bool:
    if guild_id is None:
        return False

    billing_store = getattr(bot, "billing_store", None)
    if interaction is not None and billing_store is not None and hasattr(billing_store, "sync_from_entitlements"):
        assignment = billing_store.sync_from_entitlements(guild_id, getattr(interaction, "entitlements", []))
        if assignment.get("is_active"):
            return True

    controls = getattr(bot, "command_controls", None)
    return bool(controls and hasattr(controls, "is_premium_enabled") and controls.is_premium_enabled(guild_id))


def command_limit(limit: int, *, premium_enabled: bool) -> int | None:
    return None if premium_enabled else max(int(limit), 0)


def limit_reached_message(item_label: str, limit: int) -> str:
    return (
        f"Free servers can save up to {int(limit)} {item_label}. "
        "Turn Premium on to remove this limit."
    )


def usage_footer(item_count: int, item_label: str, limit: int, *, premium_enabled: bool) -> str:
    if premium_enabled:
        return f"{int(item_count)} {item_label} saved. Premium removes the cap here."
    return f"{int(item_count)}/{int(limit)} free-plan {item_label} used"
