import discord
from discord import app_commands
from discord.ext import commands


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
        description="Delete a batch of recent messages"
    )
    @app_commands.describe(
        amount="Number of recent messages to scan",
        user="Optional: delete only this user's messages"
    )
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: int,
        user: discord.Member = None
    ):
        await interaction.response.defer(ephemeral=True)

        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.followup.send(
                "This command only works in a server.",
                ephemeral=True
            )

        if not self.can_manage(interaction.user):
            return await interaction.followup.send(
                "You need Manage Messages or Administrator to use purge.",
                ephemeral=True
            )

        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.followup.send(
                "Purge only works in text channels.",
                ephemeral=True
            )

        configured_limit = self.bot.command_controls.get_purge_limit(interaction.guild.id)
        if amount < 1 or amount > configured_limit:
            return await interaction.followup.send(
                f"Choose a number between 1 and {configured_limit}.",
                ephemeral=True
            )

        def check(message: discord.Message):
            if user:
                return message.author.id == user.id
            return True

        try:
            deleted = await interaction.channel.purge(
                limit=amount,
                check=check
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                "I need Manage Messages in this channel to do that.",
                ephemeral=True
            )
        except discord.HTTPException:
            return await interaction.followup.send(
                "I couldn't finish the purge right now.",
                ephemeral=True
            )

        message = f"Deleted {len(deleted)} messages"
        if user:
            message += f" from {user.mention}"

        await interaction.followup.send(message, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Purge(bot))
