import discord
from discord.ext import commands
from discord import app_commands


class Lock(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def check_permissions(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.",
                ephemeral=True
            )
            return False

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ I couldn't verify your server permissions.",
                ephemeral=True
            )
            return False

        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "❌ You need **Manage Channels** to use this command.",
                ephemeral=True
            )
            return False

        bot_member = interaction.guild.me
        if bot_member is None:
            await interaction.response.send_message(
                "❌ I couldn't verify my own permissions.",
                ephemeral=True
            )
            return False

        if not bot_member.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "❌ I need **Manage Channels** to do that.",
                ephemeral=True
            )
            return False

        return True

    @app_commands.command(
        name="lock",
        description="Lock a channel so @everyone cannot send messages"
    )
    @app_commands.describe(
        channel="The channel to lock (leave blank for current channel)",
        reason="Optional reason for locking the channel"
    )
    async def lock(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        reason: str | None = None
    ):
        if not await self.check_permissions(interaction):
            return

        assert interaction.guild is not None
        target_channel = channel or interaction.channel

        if not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ That is not a text channel.",
                ephemeral=True
            )
            return

        overwrite = target_channel.overwrites_for(interaction.guild.default_role)
        already_locked = overwrite.send_messages is False

        if already_locked:
            await interaction.response.send_message(
                f"🔒 {target_channel.mention} is already locked.",
                ephemeral=True
            )
            return

        overwrite.send_messages = False

        try:
            await target_channel.set_permissions(
                interaction.guild.default_role,
                overwrite=overwrite,
                reason=reason or f"Locked by {interaction.user}"
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Discord blocked me from changing that channel's permissions.",
                ephemeral=True
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ Something went wrong while locking the channel.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🔒 Channel Locked",
            description=f"{target_channel.mention} has been locked.",
            color=discord.Color.red()
        )
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(name="Reason", value=reason or "No reason provided.", inline=True)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="unlock",
        description="Unlock a channel so @everyone can send messages again"
    )
    @app_commands.describe(
        channel="The channel to unlock (leave blank for current channel)",
        reason="Optional reason for unlocking the channel"
    )
    async def unlock(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        reason: str | None = None
    ):
        if not await self.check_permissions(interaction):
            return

        assert interaction.guild is not None
        target_channel = channel or interaction.channel

        if not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ That is not a text channel.",
                ephemeral=True
            )
            return

        overwrite = target_channel.overwrites_for(interaction.guild.default_role)
        already_unlocked = overwrite.send_messages is not False

        if already_unlocked:
            await interaction.response.send_message(
                f"🔓 {target_channel.mention} is already unlocked.",
                ephemeral=True
            )
            return

        overwrite.send_messages = None

        try:
            await target_channel.set_permissions(
                interaction.guild.default_role,
                overwrite=overwrite,
                reason=reason or f"Unlocked by {interaction.user}"
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Discord blocked me from changing that channel's permissions.",
                ephemeral=True
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ Something went wrong while unlocking the channel.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🔓 Channel Unlocked",
            description=f"{target_channel.mention} has been unlocked.",
            color=discord.Color.green()
        )
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(name="Reason", value=reason or "No reason provided.", inline=True)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="slowmode",
        description="Set the slowmode delay for a text channel"
    )
    @app_commands.describe(
        seconds="Slowmode in seconds (0 to turn it off, max 21600)",
        channel="The channel to update (leave blank for current channel)",
        reason="Optional reason for changing slowmode"
    )
    async def slowmode(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21600],
        channel: discord.TextChannel | None = None,
        reason: str | None = None
    ):
        if not await self.check_permissions(interaction):
            return

        target_channel = channel or interaction.channel

        if not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ That is not a text channel.",
                ephemeral=True
            )
            return

        current_delay = target_channel.slowmode_delay or 0
        if current_delay == seconds:
            if seconds == 0:
                msg = f"🐢 Slowmode is already off in {target_channel.mention}."
            else:
                msg = f"🐢 {target_channel.mention} is already set to `{seconds}` seconds slowmode."
            await interaction.response.send_message(msg, ephemeral=True)
            return

        try:
            await target_channel.edit(
                slowmode_delay=seconds,
                reason=reason or f"Slowmode changed by {interaction.user}"
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Discord blocked me from changing slowmode in that channel.",
                ephemeral=True
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ Something went wrong while changing slowmode.",
                ephemeral=True
            )
            return

        if seconds == 0:
            title = "🐢 Slowmode Disabled"
            description = f"Slowmode has been turned off in {target_channel.mention}."
            color = discord.Color.green()
        else:
            title = "🐢 Slowmode Updated"
            description = f"Slowmode in {target_channel.mention} is now set to `{seconds}` seconds."
            color = discord.Color.orange()

        embed = discord.Embed(
            title=title,
            description=description,
            color=color
        )
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(name="Reason", value=reason or "No reason provided.", inline=True)

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Lock(bot))