import discord
from discord.ext import commands
from discord import app_commands


class MemberCount(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="membercount",
        description="Show the member count for this server"
    )
    async def membercount(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.",
                ephemeral=True
            )
            return

        guild = interaction.guild
        total_members = guild.member_count or 0

        humans = 0
        bots = 0
        online_like = 0

        for member in guild.members:
            if member.bot:
                bots += 1
            else:
                humans += 1

            if member.status != discord.Status.offline:
                online_like += 1

        created_ts = int(guild.created_at.timestamp())

        embed = discord.Embed(
            title=f"👥 Member Count — {guild.name}",
            description="A quick look at this server's members.",
            color=discord.Color.blurple()
        )

        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.add_field(name="Total Members", value=f"`{total_members:,}`", inline=True)
        embed.add_field(name="Humans", value=f"`{humans:,}`", inline=True)
        embed.add_field(name="Bots", value=f"`{bots:,}`", inline=True)

        embed.add_field(name="Online Now", value=f"`{online_like:,}`", inline=True)
        embed.add_field(name="Server ID", value=f"`{guild.id}`", inline=True)
        embed.add_field(name="Created", value=f"<t:{created_ts}:R>", inline=True)

        embed.set_footer(
            text=f"Requested by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url
        )

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(MemberCount(bot))