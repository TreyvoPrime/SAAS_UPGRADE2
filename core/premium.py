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
}


def normalize_tier(value: str | None) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in VALID_TIERS else TIER_FREE


def tier_label(value: str | None) -> str:
    return "Premium" if normalize_tier(value) == TIER_PREMIUM else "Free"


def premium_required_message(feature_name: str) -> str:
    return f"{feature_name} is part of ServerCore Premium right now."
