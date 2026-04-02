from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands
from discord import app_commands
from core.storage import read_json, write_json

DATA_FILE = Path("serverstats_data.json")


def load_data() -> dict:
    data = read_json(DATA_FILE, {})
    return data if isinstance(data, dict) else {}


def save_data(data: dict) -> None:
    write_json(DATA_FILE, data)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def today_key() -> str:
    return utc_now().strftime("%Y-%m-%d")


def iso_week_key() -> str:
    now = utc_now()
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


class ServerStats(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = load_data()

    # ----------------------------
    # DATA HELPERS
    # ----------------------------
    def ensure_guild_entry(self, guild_id: int) -> None:
        guild_id = str(guild_id)

        if guild_id not in self.data:
            self.data[guild_id] = {
                "total_messages": 0,
                "channels": {},
                "users": {},
                "daily": {},
                "weekly": {}
            }

    def cleanup_old_stats(self, guild_id: int) -> None:
        guild_id = str(guild_id)
        self.ensure_guild_entry(int(guild_id))

        guild_data = self.data[guild_id]
        current_day = today_key()
        current_week = iso_week_key()

        daily_keys = list(guild_data.get("daily", {}).keys())
        for key in daily_keys:
            if key != current_day:
                try:
                    key_date = datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    age_days = (utc_now() - key_date).days
                    if age_days > 30:
                        del guild_data["daily"][key]
                except Exception:
                    pass

        weekly_keys = list(guild_data.get("weekly", {}).keys())
        for key in weekly_keys:
            if key != current_week:
                parts = key.split("-W")
                if len(parts) == 2:
                    try:
                        year = int(parts[0])
                        week = int(parts[1])

                        current_year, current_week_num, _ = utc_now().isocalendar()
                        week_diff = (current_year - year) * 52 + (current_week_num - week)

                        if week_diff > 12:
                            del guild_data["weekly"][key]
                    except Exception:
                        pass

    def increment_message_count(self, guild_id: int, channel_id: int, user_id: int) -> None:
        guild_id_str = str(guild_id)
        channel_id_str = str(channel_id)
        user_id_str = str(user_id)

        self.ensure_guild_entry(guild_id)
        self.cleanup_old_stats(guild_id)

        guild_data = self.data[guild_id_str]
        current_day = today_key()
        current_week = iso_week_key()

        guild_data["total_messages"] += 1

        if channel_id_str not in guild_data["channels"]:
            guild_data["channels"][channel_id_str] = 0
        guild_data["channels"][channel_id_str] += 1

        if user_id_str not in guild_data["users"]:
            guild_data["users"][user_id_str] = 0
        guild_data["users"][user_id_str] += 1

        if current_day not in guild_data["daily"]:
            guild_data["daily"][current_day] = 0
        guild_data["daily"][current_day] += 1

        if current_week not in guild_data["weekly"]:
            guild_data["weekly"][current_week] = 0
        guild_data["weekly"][current_week] += 1

        save_data(self.data)

    def get_total_messages(self, guild_id: int) -> int:
        self.ensure_guild_entry(guild_id)
        return self.data[str(guild_id)].get("total_messages", 0)

    def get_messages_today(self, guild_id: int) -> int:
        self.ensure_guild_entry(guild_id)
        return self.data[str(guild_id)].get("daily", {}).get(today_key(), 0)

    def get_messages_this_week(self, guild_id: int) -> int:
        self.ensure_guild_entry(guild_id)
        return self.data[str(guild_id)].get("weekly", {}).get(iso_week_key(), 0)

    def get_top_channel(self, guild: discord.Guild):
        self.ensure_guild_entry(guild.id)
        channels = self.data[str(guild.id)].get("channels", {})

        if not channels:
            return None, 0

        top_channel_id, count = max(channels.items(), key=lambda item: item[1])
        channel = guild.get_channel(int(top_channel_id))
        return channel, count

    def get_top_user(self, guild: discord.Guild):
        self.ensure_guild_entry(guild.id)
        users = self.data[str(guild.id)].get("users", {})

        if not users:
            return None, 0

        top_user_id, count = max(users.items(), key=lambda item: item[1])
        member = guild.get_member(int(top_user_id))
        return member, count

    # ----------------------------
    # EVENT LISTENER
    # ----------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return

        if message.author.bot:
            return

        self.increment_message_count(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            user_id=message.author.id
        )

    # ----------------------------
    # COMMAND
    # ----------------------------
    @app_commands.command(
        name="serverstats",
        description="View information and statistics about this server"
    )
    async def serverstats(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.",
                ephemeral=True
            )
            return

        guild = interaction.guild
        self.ensure_guild_entry(guild.id)
        self.cleanup_old_stats(guild.id)

        total_members = guild.member_count or 0
        bot_count = sum(1 for member in guild.members if member.bot)
        human_count = total_members - bot_count

        text_channels = len(guild.text_channels)
        voice_channels = len(guild.voice_channels)
        categories = len(guild.categories)
        roles = len(guild.roles) - 1 if len(guild.roles) > 0 else 0
        emojis = len(guild.emojis)

        owner = guild.owner.mention if guild.owner else "Unknown"
        afk_channel = guild.afk_channel.mention if guild.afk_channel else "None"
        system_channel = guild.system_channel.mention if guild.system_channel else "None"
        verification_level = str(guild.verification_level).replace("_", " ").title()

        total_messages = self.get_total_messages(guild.id)
        messages_today = self.get_messages_today(guild.id)
        messages_this_week = self.get_messages_this_week(guild.id)

        top_channel, top_channel_count = self.get_top_channel(guild)
        top_user, top_user_count = self.get_top_user(guild)

        if top_channel is not None:
            top_channel_text = f"🏆 {top_channel.mention}\n`{top_channel_count:,}` messages"
        else:
            top_channel_text = "No tracked messages yet"

        if top_user is not None:
            top_user_text = f"🏆 {top_user.mention}\n`{top_user_count:,}` messages"
        else:
            top_user_text = "No tracked users yet"

        created_timestamp = int(guild.created_at.timestamp())

        embed = discord.Embed(
            title=f"📊 {guild.name}",
            description="**Server Statistics Overview**",
            color=discord.Color.blurple()
        )

        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        if guild.banner:
            embed.set_image(url=guild.banner.url)

        embed.add_field(
            name="🆔 Server Info",
            value=(
                f"**Owner:** {owner}\n"
                f"**Server ID:** `{guild.id}`\n"
                f"**Created:** <t:{created_timestamp}:F>\n"
                f"**Age:** <t:{created_timestamp}:R>"
            ),
            inline=False
        )

        embed.add_field(
            name="👥 Members",
            value=(
                f"**Total:** `{total_members:,}`\n"
                f"**Humans:** `{human_count:,}`\n"
                f"**Bots:** `{bot_count:,}`"
            ),
            inline=True
        )

        embed.add_field(
            name="💬 Channels",
            value=(
                f"**Text:** `{text_channels}`\n"
                f"**Voice:** `{voice_channels}`\n"
                f"**Categories:** `{categories}`"
            ),
            inline=True
        )

        embed.add_field(
            name="🌟 Server Extras",
            value=(
                f"**Roles:** `{roles}`\n"
                f"**Emojis:** `{emojis}`\n"
                f"**Verification:** `{verification_level}`"
            ),
            inline=True
        )

        embed.add_field(
            name="🚀 Boost Status",
            value=(
                f"**Boosts:** `{guild.premium_subscription_count or 0}`\n"
                f"**Tier:** `{guild.premium_tier}`\n"
                f"**AFK Channel:** {afk_channel}"
            ),
            inline=True
        )

        embed.add_field(
            name="⚙️ System",
            value=(
                f"**System Channel:** {system_channel}\n"
                f"**Locale:** `{getattr(guild.preferred_locale, 'value', guild.preferred_locale)}`"
            ),
            inline=True
        )

        embed.add_field(
            name="📈 Activity Tracking",
            value=(
                f"**Tracked Messages:** `{total_messages:,}`\n"
                f"**Messages Today:** `{messages_today:,}`\n"
                f"**Messages This Week:** `{messages_this_week:,}`"
            ),
            inline=True
        )

        embed.add_field(
            name="🔥 Top Channel",
            value=top_channel_text,
            inline=False
        )

        embed.add_field(
            name="👑 Most Active User",
            value=top_user_text,
            inline=False
        )

        embed.set_footer(
            text="Tracked message stats only count messages after this feature was added.",
            icon_url=interaction.user.display_avatar.url
        )

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerStats(bot))
