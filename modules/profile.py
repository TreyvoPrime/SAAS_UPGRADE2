import discord
from discord.ext import commands
from discord import app_commands


class Profile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="avatar",
        description="View a user's avatar"
    )
    @app_commands.describe(user="The user whose avatar you want to view")
    async def avatar(
        self,
        interaction: discord.Interaction,
        user: discord.Member | discord.User | None = None
    ):
        target = user or interaction.user

        embed = discord.Embed(
            title=f"🖼️ {target.display_name if hasattr(target, 'display_name') else target.name}'s Avatar",
            color=discord.Color.blurple()
        )

        embed.set_image(url=target.display_avatar.url)
        embed.add_field(
            name="Avatar Link",
            value=f"[Click here to open avatar]({target.display_avatar.url})",
            inline=False
        )
        embed.set_footer(
            text=f"Requested by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="banner",
        description="View a user's banner"
    )
    @app_commands.describe(user="The user whose banner you want to view")
    async def banner(
        self,
        interaction: discord.Interaction,
        user: discord.Member | discord.User | None = None
    ):
        target = user or interaction.user

        try:
            fetched_user = await self.bot.fetch_user(target.id)
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ I couldn't fetch that user's profile data.",
                ephemeral=True
            )
            return

        if fetched_user.banner is None:
            await interaction.response.send_message(
                "❌ That user does not have a banner set.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"🖼️ {fetched_user.name}'s Banner",
            color=discord.Color.blurple()
        )

        embed.set_image(url=fetched_user.banner.url)
        embed.add_field(
            name="Banner Link",
            value=f"[Click here to open banner]({fetched_user.banner.url})",
            inline=False
        )
        embed.set_footer(
            text=f"Requested by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url
        )

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Profile(bot))