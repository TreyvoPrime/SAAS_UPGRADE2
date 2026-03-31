from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class RoleManager(commands.Cog):
    role = app_commands.Group(
        name="role",
        description="Give or remove roles from members",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _require_role_staff(self, interaction: discord.Interaction) -> discord.Member | None:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return None
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("I couldn't verify your server permissions.", ephemeral=True)
            return None
        if not (interaction.user.guild_permissions.manage_roles or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("You need Manage Roles or Administrator to use this.", ephemeral=True)
            return None
        return interaction.user

    async def _validate_role_action(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        role: discord.Role,
        *,
        action_name: str,
    ) -> bool:
        assert interaction.guild is not None
        assert isinstance(interaction.user, discord.Member)
        me = interaction.guild.me
        if me is None or not me.guild_permissions.manage_roles:
            await interaction.response.send_message("I need Manage Roles before I can do that.", ephemeral=True)
            return False
        if role.is_default():
            await interaction.response.send_message("That role can't be managed this way.", ephemeral=True)
            return False
        if role >= me.top_role:
            await interaction.response.send_message("That role is above my top role, so Discord will block the change.", ephemeral=True)
            return False
        if interaction.guild.owner_id != interaction.user.id and role >= interaction.user.top_role:
            await interaction.response.send_message(f"You can only {action_name} roles below your top role.", ephemeral=True)
            return False
        if member.id == interaction.guild.owner_id and interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("Only the server owner can change roles on the server owner.", ephemeral=True)
            return False
        if member != interaction.user and interaction.guild.owner_id != interaction.user.id and member.top_role >= interaction.user.top_role:
            await interaction.response.send_message("You can only manage roles for members below your top role.", ephemeral=True)
            return False
        if member.top_role >= me.top_role and member.id != interaction.guild.owner_id:
            await interaction.response.send_message("I can only manage roles for members below my top role.", ephemeral=True)
            return False
        return True

    async def _log_role_event(
        self,
        guild: discord.Guild,
        *,
        title: str,
        description: str,
        user_name: str | None = None,
        channel_name: str | None = None,
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
                channel_name=channel_name,
                fields=fields,
            )

    @role.command(name="add", description="Give a role to a member")
    async def role_add(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        moderator = await self._require_role_staff(interaction)
        if moderator is None or interaction.guild is None:
            return
        if not await self._validate_role_action(interaction, member, role, action_name="assign"):
            return
        if role in member.roles:
            await interaction.response.send_message(f"{member.mention} already has {role.mention}.", ephemeral=True)
            return
        await member.add_roles(role, reason=f"{interaction.user} used /role add")
        await interaction.response.send_message(f"Gave {role.mention} to {member.mention}.", ephemeral=True)
        await self._log_role_event(
            interaction.guild,
            title="Role Granted",
            description=f"{role.mention} was added to {member.mention}.",
            user_name=str(interaction.user),
            channel_name=getattr(interaction.channel, "name", None),
            fields=[("Role", role.name, True), ("Member", str(member), True)],
        )

    @role.command(name="remove", description="Remove a role from a member")
    async def role_remove(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        moderator = await self._require_role_staff(interaction)
        if moderator is None or interaction.guild is None:
            return
        if not await self._validate_role_action(interaction, member, role, action_name="remove"):
            return
        if role not in member.roles:
            await interaction.response.send_message(f"{member.mention} does not have {role.mention}.", ephemeral=True)
            return
        await member.remove_roles(role, reason=f"{interaction.user} used /role remove")
        await interaction.response.send_message(f"Removed {role.mention} from {member.mention}.", ephemeral=True)
        await self._log_role_event(
            interaction.guild,
            title="Role Removed",
            description=f"{role.mention} was removed from {member.mention}.",
            user_name=str(interaction.user),
            channel_name=getattr(interaction.channel, "name", None),
            fields=[("Role", role.name, True), ("Member", str(member), True)],
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(RoleManager(bot))
