from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from core.storage import read_json, write_json

DATA_FILE = Path("afk_data.json")
AFK_PREFIX = "[AFK] "


def load_data() -> dict:
    data = read_json(DATA_FILE, {})
    return data if isinstance(data, dict) else {}


def save_data(data: dict) -> None:
    write_json(DATA_FILE, data)


class AFK(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = load_data()

    def _refresh(self) -> None:
        self.data = load_data()

    def ensure_guild_entry(self, guild_id: int) -> None:
        guild_key = str(guild_id)
        if guild_key not in self.data:
            self.data[guild_key] = {}

    def get_guild_data(self, guild_id: int) -> dict:
        self._refresh()
        self.ensure_guild_entry(guild_id)
        return self.data[str(guild_id)]

    def get_user_afk(self, guild_id: int, user_id: int):
        return self.get_guild_data(guild_id).get(str(user_id))

    def set_user_afk(self, guild_id: int, user_id: int, afk_info: dict) -> None:
        guild_data = self.get_guild_data(guild_id)
        guild_data[str(user_id)] = afk_info
        save_data(self.data)

    def remove_user_afk(self, guild_id: int, user_id: int) -> bool:
        guild_data = self.get_guild_data(guild_id)
        user_key = str(user_id)
        if user_key not in guild_data:
            return False
        del guild_data[user_key]
        save_data(self.data)
        return True

    async def try_add_afk_nick(self, member: discord.Member) -> None:
        if member.guild is None or member.guild.me is None:
            return
        if not member.guild.me.guild_permissions.manage_nicknames:
            return
        if member.top_role >= member.guild.me.top_role:
            return

        current_nick = member.nick or member.name
        if current_nick.startswith(AFK_PREFIX):
            return

        new_nick = f"{AFK_PREFIX}{current_nick}"
        if len(new_nick) > 32:
            return
        try:
            await member.edit(nick=new_nick, reason="User set AFK")
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def try_remove_afk_nick(self, member: discord.Member) -> None:
        if member.guild is None or member.guild.me is None:
            return
        if not member.guild.me.guild_permissions.manage_nicknames:
            return
        if member.top_role >= member.guild.me.top_role:
            return

        current_nick = member.nick
        if not current_nick or not current_nick.startswith(AFK_PREFIX):
            return

        restored_nick = current_nick[len(AFK_PREFIX):]
        try:
            await member.edit(nick=restored_nick or None, reason="User returned from AFK")
        except (discord.Forbidden, discord.HTTPException):
            pass

    @app_commands.command(name="afk", description="Mark yourself as away with an optional reason")
    @app_commands.describe(reason="Why you are away")
    async def afk(
        self,
        interaction: discord.Interaction,
        reason: app_commands.Range[str, 1, 200] | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("I couldn't verify your member data in this server.", ephemeral=True)
            return

        afk_reason = reason.strip() if reason else "Away right now"
        self.set_user_afk(
            interaction.guild.id,
            interaction.user.id,
            {"reason": afk_reason, "since": int(discord.utils.utcnow().timestamp())},
        )
        await self.try_add_afk_nick(interaction.user)

        embed = discord.Embed(
            title="AFK enabled",
            description=f"You are now marked as away.\nReason: {afk_reason}",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Your AFK status will clear automatically when you send a message.")
        await interaction.response.send_message(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or not isinstance(message.author, discord.Member):
            return

        guild_id = message.guild.id
        author_id = message.author.id

        user_afk = self.get_user_afk(guild_id, author_id)
        if user_afk is not None:
            self.remove_user_afk(guild_id, author_id)
            await self.try_remove_afk_nick(message.author)

            description = "Welcome back. Your AFK status has been removed."
            since = user_afk.get("since")
            if since:
                description += f"\nYou were away since <t:{since}:R>."

            embed = discord.Embed(
                title="AFK cleared",
                description=description,
                color=discord.Color.green(),
            )
            try:
                await message.channel.send(embed=embed, delete_after=8)
            except discord.HTTPException:
                pass

        if not message.mentions:
            return

        already_notified: set[int] = set()
        for mentioned_user in message.mentions:
            if mentioned_user.bot or mentioned_user.id in already_notified:
                continue

            afk_data = self.get_user_afk(guild_id, mentioned_user.id)
            if afk_data is None:
                continue

            already_notified.add(mentioned_user.id)
            reason = afk_data.get("reason", "Away right now")
            since = afk_data.get("since")

            value = f"Reason: {reason}"
            if since:
                value += f"\nSince: <t:{since}:R>"

            embed = discord.Embed(
                title="User is AFK",
                description=f"{mentioned_user.mention} is currently away.",
                color=discord.Color.orange(),
            )
            embed.add_field(name="Status", value=value, inline=False)
            try:
                await message.channel.send(embed=embed, delete_after=8)
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AFK(bot))
