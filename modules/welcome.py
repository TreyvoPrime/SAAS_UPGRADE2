from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class WelcomeLeave(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _require_manager(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return False

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("I couldn't verify your server permissions.", ephemeral=True)
            return False

        if not (
            interaction.user.guild_permissions.manage_guild
            or interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server or Administrator to update welcome and leave settings.",
                ephemeral=True,
            )
            return False

        return True

    def _target_channel(
        self,
        interaction: discord.Interaction,
        explicit_channel: discord.TextChannel | None,
    ) -> discord.TextChannel | None:
        if explicit_channel is not None:
            return explicit_channel
        return interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None

    @app_commands.command(
        name="setwelcome",
        description="Choose which channel welcome messages are sent in",
    )
    @app_commands.describe(
        channel="Channel to send welcome messages in. Leave blank to use the current channel.",
    )
    async def setwelcome(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if not await self._require_manager(interaction):
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        target_channel = self._target_channel(interaction, channel)
        if target_channel is None:
            await interaction.response.send_message(
                "Pick a text channel, or run this command inside the channel you want to use for welcome messages.",
                ephemeral=True,
            )
            return

        self.bot.greetings.set_welcome(
            guild.id,
            channel_id=target_channel.id,
        )
        await interaction.response.send_message(
            f"Welcome messages will be sent in {target_channel.mention}. Edit the welcome text from the dashboard when you want to customize it.",
            ephemeral=True,
        )

    @app_commands.command(
        name="setleave",
        description="Choose which channel leave messages are sent in",
    )
    @app_commands.describe(
        channel="Channel to send leave messages in. Leave blank to use the current channel.",
    )
    async def setleave(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if not await self._require_manager(interaction):
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        target_channel = self._target_channel(interaction, channel)
        if target_channel is None:
            await interaction.response.send_message(
                "Pick a text channel, or run this command inside the channel you want to use for leave messages.",
                ephemeral=True,
            )
            return

        self.bot.greetings.set_leave(
            guild.id,
            channel_id=target_channel.id,
        )
        await interaction.response.send_message(
            f"Leave messages will be sent in {target_channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="setjoindm",
        description="Turn the join guide DM on or off for new members",
    )
    @app_commands.describe(enabled="Whether new members should get the join guide DM")
    async def setjoindm(
        self,
        interaction: discord.Interaction,
        enabled: bool,
    ) -> None:
        if not await self._require_manager(interaction):
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        self.bot.greetings.set_join_dm(guild.id, enabled=enabled)
        await interaction.response.send_message(
            "Join guide DMs are now on." if enabled else "Join guide DMs are now off.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeLeave(bot))
