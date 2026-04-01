from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class Nickname(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setnickname", description="Set or reset a member's server nickname")
    @app_commands.describe(
        member="The member whose nickname you want to change",
        nickname="Leave blank to reset their nickname",
    )
    async def setnickname(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        nickname: app_commands.Range[str, 1, 32] | None = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command only works inside a server.", ephemeral=True)
            return

        actor = interaction.user
        if not (actor.guild_permissions.manage_nicknames or actor.guild_permissions.administrator):
            await interaction.response.send_message("You need Manage Nicknames to use this command.", ephemeral=True)
            return

        me = interaction.guild.me
        if me is None or not (me.guild_permissions.manage_nicknames or me.guild_permissions.administrator):
            await interaction.response.send_message("I need Manage Nicknames to do that.", ephemeral=True)
            return

        if member == interaction.guild.owner:
            await interaction.response.send_message("I can't change the server owner's nickname.", ephemeral=True)
            return

        if actor != interaction.guild.owner and member.top_role >= actor.top_role:
            await interaction.response.send_message("You can only change nicknames for members below your top role.", ephemeral=True)
            return

        if member.top_role >= me.top_role:
            await interaction.response.send_message("That member's top role is higher than or equal to mine, so I can't change their nickname.", ephemeral=True)
            return

        try:
            await member.edit(nick=nickname, reason=f"Nickname updated by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message("I couldn't change that nickname because of role hierarchy or missing permissions.", ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.response.send_message("I couldn't update that nickname right now. Please try again.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Updated {member.mention}'s nickname to `{nickname}`." if nickname else f"Reset {member.mention}'s nickname.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Nickname(bot))
