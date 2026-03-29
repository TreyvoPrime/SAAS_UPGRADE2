import discord
from discord.ext import commands
from discord import app_commands
import json
import os


DATA_FILE = "welcome_channels.json"


# ----------------------------
# LOAD / SAVE HELPERS
# ----------------------------
def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)


class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data = load_data()

    # ----------------------------
    # SET WELCOME CHANNEL
    # ----------------------------
    @app_commands.command(
        name="setwelcome",
        description="Set the welcome channel for this server"
    )
    async def setwelcome(self, interaction: discord.Interaction):

        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "❌ You need administrator permissions.",
                ephemeral=True
            )

        guild_id = str(interaction.guild.id)
        channel_id = interaction.channel.id

        self.data[guild_id] = channel_id
        save_data(self.data)

        await interaction.response.send_message(
            f"✅ Welcome channel set to {interaction.channel.mention}",
            ephemeral=True
        )

    # ----------------------------
    # MEMBER JOIN EVENT
    # ----------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):

        guild_id = str(member.guild.id)
        channel_id = self.data.get(guild_id)

        if not channel_id:
            return

        channel = member.guild.get_channel(channel_id)

        if channel is None:
            return

        embed = discord.Embed(
            title="👋 Welcome!",
            description=f"Hey {member.mention}, welcome to **{member.guild.name}**!",
            color=discord.Color.green()
        )

        embed.add_field(
            name="📌 Info",
            value="Read the rules and enjoy your stay!",
            inline=False
        )

        embed.set_thumbnail(url=member.display_avatar.url)

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass


# ----------------------------
# THIS MUST BE OUTSIDE THE CLASS
# ----------------------------
async def setup(bot):
    await bot.add_cog(Welcome(bot))