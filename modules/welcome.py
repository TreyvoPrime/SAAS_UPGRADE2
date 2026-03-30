from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.greetings import DEFAULT_LEAVE_MESSAGE, DEFAULT_WELCOME_MESSAGE


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

    def _state_embed(self, guild_id: int) -> discord.Embed:
        guild = self.bot.get_guild(guild_id)
        channel_lookup = {channel.id: f"#{channel.name}" for channel in getattr(guild, "text_channels", [])}
        state = self.bot.greetings.get_dashboard_state(guild_id, channel_lookup)

        embed = discord.Embed(
            title="Welcome / Leave settings updated",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Welcome",
            value=(
                f"Channel: {state['welcome']['channel_name']}\n"
                f"Message: {state['welcome']['message']}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Leave",
            value=(
                f"Channel: {state['leave']['channel_name']}\n"
                f"Message: {state['leave']['message']}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Placeholders",
            value="`{user}` `{user_name}` `{display_name}` `{server}` `{membercount}`",
            inline=False,
        )
        return embed

    @app_commands.command(
        name="setwelcome",
        description="Set the welcome message and optionally the welcome channel",
    )
    @app_commands.describe(
        message="Custom welcome message. Supports {user}, {server}, {membercount}, and more.",
        channel="Channel to send welcome messages in. Leave blank to keep the current channel.",
    )
    async def setwelcome(
        self,
        interaction: discord.Interaction,
        message: str | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if not await self._require_manager(interaction):
            return

        assert interaction.guild is not None
        existing = self.bot.greetings.store.get_guild(interaction.guild.id)
        target_channel = channel or interaction.channel if isinstance(interaction.channel, discord.TextChannel) else channel
        next_channel = target_channel.id if target_channel is not None else existing["welcome_channel_id"]
        next_message = (message or existing["welcome_message"] or DEFAULT_WELCOME_MESSAGE).strip()

        self.bot.greetings.set_welcome(
            interaction.guild.id,
            channel_id=next_channel,
            message=next_message,
        )
        await interaction.response.send_message(
            embed=self._state_embed(interaction.guild.id),
            ephemeral=True,
        )

    @app_commands.command(
        name="setleave",
        description="Set the leave channel and optionally the leave message",
    )
    @app_commands.describe(
        channel="Channel to send leave messages in. Leave blank to keep the current one.",
        message="Custom leave message. Supports {user_name}, {server}, and more.",
    )
    async def setleave(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        message: str | None = None,
    ) -> None:
        if not await self._require_manager(interaction):
            return

        assert interaction.guild is not None
        existing = self.bot.greetings.store.get_guild(interaction.guild.id)
        target_channel = channel or interaction.channel if isinstance(interaction.channel, discord.TextChannel) else channel
        next_channel = target_channel.id if target_channel is not None else existing["leave_channel_id"]
        next_message = (message or existing["leave_message"] or DEFAULT_LEAVE_MESSAGE).strip()

        self.bot.greetings.set_leave(
            interaction.guild.id,
            channel_id=next_channel,
            message=next_message,
        )
        await interaction.response.send_message(
            embed=self._state_embed(interaction.guild.id),
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self.bot.greetings.send_welcome(member)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self.bot.greetings.send_leave(member)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeLeave(bot))
