from __future__ import annotations

import discord

from core.command_controls import CommandControlStore
from core.command_logs import CommandLogStore


class CommandAccessManager:
    def __init__(self, controls: CommandControlStore, logs: CommandLogStore):
        self.controls = controls
        self.logs = logs

    @staticmethod
    def _blocked_roles_message(interaction: discord.Interaction, allowed_role_ids: set[int]) -> str:
        guild = interaction.guild
        if guild is None or not allowed_role_ids:
            return "Your roles are not allowed to use this command. Ask a server admin to update Command Access."

        role_mentions = []
        for role_id in sorted(allowed_role_ids):
            role = guild.get_role(role_id)
            if role is not None:
                role_mentions.append(role.mention)

        if not role_mentions:
            return "Your roles are not allowed to use this command. Ask a server admin to review Command Access."

        return (
            "You do not have access to this command right now. "
            f"Allowed roles: {', '.join(role_mentions)}."
        )

    async def enforce(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.command is None:
            return True

        if not isinstance(interaction.user, discord.Member):
            return True

        command_name = interaction.command.qualified_name
        policy = self.controls.get_policy(interaction.guild.id, command_name)

        if not policy["enabled"]:
            self.logs.append(
                {
                    "guild_id": interaction.guild.id,
                    "guild_name": interaction.guild.name,
                    "command": command_name,
                    "status": "blocked_disabled",
                    "user_id": interaction.user.id,
                    "user_name": str(interaction.user),
                }
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "This command is disabled for this server right now.",
                    ephemeral=True,
                )
            return False

        if interaction.user.guild_permissions.administrator:
            return True

        allowed_role_ids = set(policy["allowed_role_ids"])
        if policy.get("restrict_to_roles"):
            member_role_ids = {role.id for role in interaction.user.roles}
            if not allowed_role_ids or allowed_role_ids.isdisjoint(member_role_ids):
                self.logs.append(
                    {
                        "guild_id": interaction.guild.id,
                        "guild_name": interaction.guild.name,
                        "command": command_name,
                        "status": "blocked_roles",
                        "user_id": interaction.user.id,
                        "user_name": str(interaction.user),
                    }
                )
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        self._blocked_roles_message(interaction, allowed_role_ids),
                        ephemeral=True,
                    )
                return False
        elif allowed_role_ids and allowed_role_ids.isdisjoint({role.id for role in interaction.user.roles}):
            self.logs.append(
                {
                    "guild_id": interaction.guild.id,
                    "guild_name": interaction.guild.name,
                    "command": command_name,
                    "status": "blocked_roles",
                    "user_id": interaction.user.id,
                    "user_name": str(interaction.user),
                }
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    self._blocked_roles_message(interaction, allowed_role_ids),
                    ephemeral=True,
                )
            return False

        return True

    def log_success(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.command is None:
            return

        self.logs.append(
            {
                "guild_id": interaction.guild.id,
                "guild_name": interaction.guild.name,
                "command": interaction.command.qualified_name,
                "status": "success",
                "user_id": interaction.user.id,
                "user_name": str(interaction.user),
                "channel_id": getattr(interaction.channel, "id", None),
                "channel_name": getattr(interaction.channel, "name", "Unknown"),
            }
        )

    def log_error(self, interaction: discord.Interaction, error: Exception) -> None:
        if interaction.guild is None or interaction.command is None:
            return

        self.logs.append(
            {
                "guild_id": interaction.guild.id,
                "guild_name": interaction.guild.name,
                "command": interaction.command.qualified_name,
                "status": "error",
                "user_id": interaction.user.id,
                "user_name": str(interaction.user),
                "error": error.__class__.__name__,
            }
        )
