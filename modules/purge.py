import discord
from discord.ext import commands
from discord import app_commands


class Purge(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def can_manage(self, member: discord.Member):
        return (
            member.guild_permissions.administrator
            or member.guild_permissions.manage_messages
        )

    @app_commands.command(
        name="purge",
        description="Advanced message purge system"
    )
    @app_commands.describe(
        amount="Number of messages to scan (1–100)",
        user="Optional: delete only this user's messages"
    )
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: int,
        user: discord.Member = None
    ):
        await interaction.response.defer(ephemeral=True)

        if not self.can_manage(interaction.user):
            return await interaction.followup.send(
                "❌ No permission.",
                ephemeral=True
            )

        if amount < 1 or amount > 100:
            return await interaction.followup.send(
                "⚠️ Amount must be between 1–100.",
                ephemeral=True
            )

        def check(message: discord.Message):
            if user:
                return message.author.id == user.id
            return True

        deleted = await interaction.channel.purge(
            limit=amount,
            check=check
        )

        msg = f"🧹 Deleted {len(deleted)} messages"
        if user:
            msg += f" from {user.mention}"

        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Purge(bot))