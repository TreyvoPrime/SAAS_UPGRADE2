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

    def _channel_label(self, guild: discord.Guild, channel_id: int | None) -> str:
        if not channel_id:
            return "Not set"
        channel = guild.get_channel(channel_id)
        return channel.mention if isinstance(channel, discord.TextChannel) else f"`{channel_id}`"

    def _build_status_embed(self, guild: discord.Guild) -> discord.Embed:
        state = self.bot.greetings.store.get_guild(guild.id)
        embed = discord.Embed(
            title=f"Welcome / Leave settings for {guild.name}",
            description="See which channels are live and how new members are being greeted.",
            color=discord.Color.blurple(),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.add_field(
            name="Welcome messages",
            value=(
                f"Channel: {self._channel_label(guild, state['welcome_channel_id'])}\n"
                f"Message: {state['welcome_message']}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Leave messages",
            value=(
                f"Channel: {self._channel_label(guild, state['leave_channel_id'])}\n"
                f"Message: {state['leave_message']}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Join guide DM",
            value=(
                f"Enabled: {'Yes' if state['join_dm_enabled'] else 'No'}\n"
                f"Message: {state['join_dm_message']}"
            ),
            inline=False,
        )
        return embed

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

        self.bot.greetings.set_welcome(guild.id, channel_id=target_channel.id)
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

        self.bot.greetings.set_leave(guild.id, channel_id=target_channel.id)
        await interaction.response.send_message(
            f"Leave messages will be sent in {target_channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(name="clearwelcome", description="Turn off welcome messages until you set a channel again")
    async def clearwelcome(self, interaction: discord.Interaction) -> None:
        if not await self._require_manager(interaction):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        self.bot.greetings.set_welcome(guild.id, channel_id=None)
        await interaction.response.send_message("Welcome messages are now off for this server.", ephemeral=True)

    @app_commands.command(name="clearleave", description="Turn off leave messages until you set a channel again")
    async def clearleave(self, interaction: discord.Interaction) -> None:
        if not await self._require_manager(interaction):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        self.bot.greetings.set_leave(guild.id, channel_id=None)
        await interaction.response.send_message("Leave messages are now off for this server.", ephemeral=True)

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

    @app_commands.command(name="welcomestatus", description="Review the current welcome, leave, and join DM setup")
    async def welcomestatus(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        await interaction.response.send_message(embed=self._build_status_embed(guild), ephemeral=True)

    @app_commands.command(name="previewgreeting", description="Preview the current welcome, leave, or join DM message")
    @app_commands.describe(
        flow="Which greeting flow you want to preview",
        member="Optional member to preview the message with",
    )
    @app_commands.choices(
        flow=[
            app_commands.Choice(name="Welcome message", value="welcome"),
            app_commands.Choice(name="Leave message", value="leave"),
            app_commands.Choice(name="Join guide DM", value="join_dm"),
        ]
    )
    async def previewgreeting(
        self,
        interaction: discord.Interaction,
        flow: app_commands.Choice[str],
        member: discord.Member | None = None,
    ) -> None:
        guild = interaction.guild
        target = member or interaction.user
        if guild is None or not isinstance(target, discord.Member):
            await interaction.response.send_message("This preview only works inside a server.", ephemeral=True)
            return

        state = self.bot.greetings.store.get_guild(guild.id)
        if flow.value == "leave":
            template = state["leave_message"]
            title = "Leave message preview"
        elif flow.value == "join_dm":
            template = state["join_dm_message"]
            title = "Join DM preview"
        else:
            template = state["welcome_message"]
            title = "Welcome message preview"

        preview = self.bot.greetings.format_message(template, target)
        embed = discord.Embed(
            title=title,
            description=preview,
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Previewing for", value=target.mention, inline=True)
        embed.add_field(name="Server", value=guild.name, inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeLeave(bot))
