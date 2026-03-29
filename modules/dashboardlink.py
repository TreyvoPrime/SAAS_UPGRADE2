import os

import discord
from discord import app_commands
from discord.ext import commands


class DashboardLink(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="dashboard",
        description="Get the dashboard link for this server"
    )
    async def dashboard(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command only works in a server.",
                ephemeral=True,
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "I could not verify your member permissions.",
                ephemeral=True,
            )
            return

        if not (
            interaction.user.guild_permissions.manage_guild
            or interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server or Administrator to use this command.",
                ephemeral=True,
            )
            return

        base_url = os.getenv("DASHBOARD_BASE_URL")
        if not base_url:
            host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
            port = os.getenv("DASHBOARD_PORT", "8000")
            base_url = f"http://{host}:{port}"

        dashboard_link = f"{base_url.rstrip('/')}/dashboard/{interaction.guild.id}"

        embed = discord.Embed(
            title="Server Dashboard",
            description=f"[Open the dashboard for **{interaction.guild.name}**]({dashboard_link})",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Direct link",
            value=dashboard_link,
            inline=False,
        )
        embed.set_footer(text="Make sure your dashboard base URL is public if members should open it outside your local machine.")

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(DashboardLink(bot))
