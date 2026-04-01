from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class Nickname(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _require_manager(self, interaction: discord.Interaction) -> discord.Member | None:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works inside a server.", ephemeral=True)
            return None
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("I couldn't verify your server permissions.", ephemeral=True)
            return None
        if not (interaction.user.guild_permissions.manage_nicknames or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("You need Manage Nicknames to use this command.", ephemeral=True)
            return None
        return interaction.user

    async def _can_edit_member(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> tuple[bool, str | None]:
        guild = interaction.guild
        actor = interaction.user
        if guild is None or not isinstance(actor, discord.Member):
            return False, "I couldn't verify the server context for that nickname change."

        me = guild.me
        if me is None or not (me.guild_permissions.manage_nicknames or me.guild_permissions.administrator):
            return False, "I need Manage Nicknames to do that."
        if member == guild.owner:
            return False, "I can't change the server owner's nickname."
        if actor != guild.owner and member.top_role >= actor.top_role:
            return False, "You can only change nicknames for members below your top role."
        if member.top_role >= me.top_role:
            return False, "That member's top role is higher than or equal to mine, so I can't change their nickname."
        return True, None

    async def _log_nickname_event(
        self,
        guild: discord.Guild,
        *,
        title: str,
        description: str,
        user_name: str | None = None,
        fields: list[tuple[str, str, bool]] | None = None,
    ) -> None:
        audit_cog = self.bot.get_cog("AuditLogCog")
        if audit_cog is not None and hasattr(audit_cog, "emit_external_event"):
            await audit_cog.emit_external_event(
                guild.id,
                title=title,
                description=description,
                status="event",
                color=discord.Color.blurple(),
                user_name=user_name,
                fields=fields,
            )

    async def _apply_nickname(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        nickname: str | None,
        *,
        reason: str | None,
        fallback_reason: str,
    ) -> None:
        moderator = await self._require_manager(interaction)
        if moderator is None or interaction.guild is None:
            return

        allowed, error_message = await self._can_edit_member(interaction, member)
        if not allowed:
            await interaction.response.send_message(error_message or "I couldn't update that nickname.", ephemeral=True)
            return

        audit_reason = reason or fallback_reason
        try:
            await member.edit(nick=nickname, reason=audit_reason)
        except discord.Forbidden:
            await interaction.response.send_message("I couldn't change that nickname because of role hierarchy or missing permissions.", ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.response.send_message("I couldn't update that nickname right now. Please try again.", ephemeral=True)
            return

        if nickname:
            message = f"Updated {member.mention}'s nickname to `{nickname}`."
            fields = [
                ("Target", member.mention, True),
                ("New nickname", nickname, True),
            ]
        else:
            message = f"Reset {member.mention}'s nickname."
            fields = [
                ("Target", member.mention, True),
                ("New nickname", "Reset to username", True),
            ]
        if reason:
            fields.append(("Reason", reason, False))

        await interaction.response.send_message(message, ephemeral=True)
        await self._log_nickname_event(
            interaction.guild,
            title="Nickname Updated",
            description=message.replace("`", ""),
            user_name=str(interaction.user),
            fields=fields,
        )

    @app_commands.command(name="setnickname", description="Set or reset a member's server nickname")
    @app_commands.describe(
        member="The member whose nickname you want to change",
        nickname="Leave blank to reset their nickname",
        reason="Optional note for your audit trail",
    )
    async def setnickname(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        nickname: app_commands.Range[str, 1, 32] | None = None,
        reason: app_commands.Range[str, 1, 120] | None = None,
    ) -> None:
        await self._apply_nickname(
            interaction,
            member,
            nickname,
            reason=reason,
            fallback_reason=f"Nickname updated by {interaction.user}",
        )

    @app_commands.command(name="resetnickname", description="Reset a member's nickname back to their username")
    @app_commands.describe(member="The member whose nickname you want to reset")
    async def resetnickname(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self._apply_nickname(
            interaction,
            member,
            None,
            reason="Nickname reset from /resetnickname",
            fallback_reason=f"Nickname reset by {interaction.user}",
        )

    @app_commands.command(name="shownickname", description="See a member's current nickname and name details")
    @app_commands.describe(member="The member whose nickname details you want to see")
    async def shownickname(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        target = member or interaction.user
        if not isinstance(target, discord.Member):
            await interaction.response.send_message("I couldn't read that member's server nickname right now.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"Nickname details for {target.display_name}",
            description="See how this member appears in the server right now.",
            color=target.color if target.color != discord.Color.default() else discord.Color.blurple(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Display name", value=target.display_name, inline=True)
        embed.add_field(name="Username", value=str(target), inline=True)
        embed.add_field(name="Nickname set", value="Yes" if target.nick else "No", inline=True)
        embed.add_field(name="Top role", value=target.top_role.mention, inline=True)
        embed.add_field(name="Server avatar", value="Custom server avatar set" if target.guild_avatar else "Using global avatar", inline=True)
        embed.add_field(name="Mention", value=target.mention, inline=True)
        embed.set_footer(text=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Nickname(bot))
