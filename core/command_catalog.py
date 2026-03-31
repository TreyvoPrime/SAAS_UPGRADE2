from __future__ import annotations

from collections.abc import Iterable

import discord
from discord import app_commands

FREE_TIER = "Free"
PREMIUM_TIER = "Premium"


def _tier_for_command(command: app_commands.Command | app_commands.Group) -> str:
    module_name = getattr(command, "module", "") or ""
    return PREMIUM_TIER if module_name.startswith("modules.premium") else FREE_TIER


def _walk_commands(
    commands: Iterable[app_commands.Command | app_commands.Group],
    module_label_map: dict[str, str],
) -> list[dict]:
    items: list[dict] = []

    for command in commands:
        if isinstance(command, app_commands.Group):
            items.extend(_walk_commands(command.commands, module_label_map))
            continue

        module_name = getattr(command, "module", "") or ""
        items.append(
            {
                "name": command.qualified_name,
                "description": command.description or "No description yet.",
                "module": module_label_map.get(module_name, module_name.split(".")[-1].replace("_", " ").title()),
                "tier": _tier_for_command(command),
            }
        )

    return sorted(items, key=lambda item: (item["tier"], item["module"], item["name"]))


def build_command_catalog(bot: discord.Client) -> list[dict]:
    module_label_map = {
        "modules.afk": "AFK",
        "modules.alert": "Alert",
        "modules.auditlog": "Audit Log",
        "modules.autoresponder": "Autoresponder",
        "modules.colors": "Colors",
        "modules.dashboardlink": "Dashboard",
        "modules.giveaway": "Giveaways",
        "modules.lock": "ServerGuard",
        "modules.membercount": "Member Count",
        "modules.poll": "Polls",
        "modules.profile": "Profile",
        "modules.purge": "ServerGuard",
        "modules.reactionroles": "Reaction Roles",
        "modules.reminders": "Reminders",
        "modules.rolemanager": "ServerGuard",
        "modules.serverdefense": "ServerGuard",
        "modules.serverstats": "Server Stats",
        "modules.support": "Support",
        "modules.userinfo": "User Info",
        "modules.welcome": "Welcome / Leave",
        "modules.premium.customcomands": "Custom Commands",
    }
    return _walk_commands(bot.tree.get_commands(guild=None), module_label_map)
