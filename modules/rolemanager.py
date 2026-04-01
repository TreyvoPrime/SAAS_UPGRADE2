from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands


def parse_role_duration(value: str) -> int:
    cleaned = (value or "").strip().lower()
    if not cleaned:
        return 0
    total = 0
    number = ""
    for char in cleaned:
        if char.isdigit():
            number += char
            continue
        if not number:
            return 0
        amount = int(number)
        number = ""
        if char == "m":
            total += amount * 60
        elif char == "h":
            total += amount * 3600
        elif char == "d":
            total += amount * 86400
        else:
            return 0
    return total if not number else 0


def format_role_duration(seconds: int) -> str:
    seconds = max(int(seconds), 0)
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


class RoleManager(commands.Cog):
    role = app_commands.Group(name="role", description="Give or remove roles from members")
    selfrole = app_commands.Group(name="selfrole", description="Let members claim approved self-assign roles")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._expiry_task: asyncio.Task | None = None

    async def cog_load(self):
        if self._expiry_task is None:
            self._expiry_task = asyncio.create_task(self._temp_role_loop(), name="temp-role-loop")

    def cog_unload(self):
        if self._expiry_task:
            self._expiry_task.cancel()
            self._expiry_task = None

    async def _temp_role_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self._expire_due_roles()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(30)

    async def _expire_due_roles(self):
        store = self.bot.temp_role_store
        now = datetime.now(timezone.utc)
        for item in store.list_items():
            try:
                expires_at = datetime.fromisoformat(str(item.get("expires_at"))).astimezone(timezone.utc)
            except Exception:
                continue
            if expires_at > now:
                continue
            guild = self.bot.get_guild(int(item["guild_id"]))
            if guild is None:
                store.remove_assignment(int(item["guild_id"]), int(item["user_id"]), int(item["role_id"]))
                continue
            member = guild.get_member(int(item["user_id"]))
            role = guild.get_role(int(item["role_id"]))
            if member is not None and role is not None and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Temporary role expired")
                except Exception:
                    continue
            store.remove_assignment(int(item["guild_id"]), int(item["user_id"]), int(item["role_id"]))

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

    async def _log_role_event(self, guild: discord.Guild, *, title: str, description: str, user_name: str | None = None, channel_name: str | None = None, fields: list[tuple[str, str, bool]] | None = None) -> None:
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

    @role.command(name="temp", description="Give a temporary role that removes itself later")
    @app_commands.describe(duration="How long the role should last, like 30m, 6h, or 2d")
    async def role_temp(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role, duration: app_commands.Range[str, 2, 20]):
        moderator = await self._require_role_staff(interaction)
        if moderator is None or interaction.guild is None:
            return
        if not await self._validate_role_action(interaction, member, role, action_name="assign"):
            return
        seconds = parse_role_duration(duration)
        if seconds <= 0:
            await interaction.response.send_message("Use a duration like `30m`, `6h`, or `2d`.", ephemeral=True)
            return
        await member.add_roles(role, reason=f"{interaction.user} used /role temp")
        expires_at = datetime.now(timezone.utc).timestamp() + seconds
        self.bot.temp_role_store.add_assignment(
            interaction.guild.id,
            member.id,
            role.id,
            expires_at=datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
            assigned_by_id=interaction.user.id,
        )
        await interaction.response.send_message(f"Gave {role.mention} to {member.mention} for {format_role_duration(seconds)}.", ephemeral=True)

    @selfrole.command(name="allow", description="Allow members to self-assign a role")
    async def selfrole_allow(self, interaction: discord.Interaction, role: discord.Role):
        moderator = await self._require_role_staff(interaction)
        if moderator is None or interaction.guild is None:
            return
        if role.is_default():
            await interaction.response.send_message("That role can't be self-assigned.", ephemeral=True)
            return
        self.bot.self_role_store.add_role(interaction.guild.id, role.id)
        await interaction.response.send_message(f"{role.mention} is now self-assignable.", ephemeral=True)

    @selfrole.command(name="deny", description="Remove a role from the self-assign list")
    async def selfrole_deny(self, interaction: discord.Interaction, role: discord.Role):
        moderator = await self._require_role_staff(interaction)
        if moderator is None or interaction.guild is None:
            return
        self.bot.self_role_store.remove_role(interaction.guild.id, role.id)
        await interaction.response.send_message(f"{role.mention} is no longer self-assignable.", ephemeral=True)

    @selfrole.command(name="list", description="Show the roles members can self-assign")
    async def selfrole_list(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        role_mentions = [
            interaction.guild.get_role(role_id).mention
            for role_id in self.bot.self_role_store.list_roles(interaction.guild.id)
            if interaction.guild.get_role(role_id) is not None
        ]
        await interaction.response.send_message(", ".join(role_mentions) if role_mentions else "No self-assign roles are configured yet.", ephemeral=True)

    @selfrole.command(name="add", description="Claim one of the approved self-assign roles")
    async def selfrole_add(self, interaction: discord.Interaction, role: discord.Role):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        allowed = set(self.bot.self_role_store.list_roles(interaction.guild.id))
        if role.id not in allowed:
            await interaction.response.send_message("That role is not available for self-assign.", ephemeral=True)
            return
        if not await self._validate_role_action(interaction, interaction.user, role, action_name="assign"):
            return
        if role in interaction.user.roles:
            await interaction.response.send_message(f"You already have {role.mention}.", ephemeral=True)
            return
        await interaction.user.add_roles(role, reason=f"{interaction.user} used /selfrole add")
        await interaction.response.send_message(f"You now have {role.mention}.", ephemeral=True)

    @selfrole.command(name="remove", description="Drop one of your self-assign roles")
    async def selfrole_remove(self, interaction: discord.Interaction, role: discord.Role):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        allowed = set(self.bot.self_role_store.list_roles(interaction.guild.id))
        if role.id not in allowed:
            await interaction.response.send_message("That role is not available for self-assign.", ephemeral=True)
            return
        if role not in interaction.user.roles:
            await interaction.response.send_message(f"You do not have {role.mention}.", ephemeral=True)
            return
        await interaction.user.remove_roles(role, reason=f"{interaction.user} used /selfrole remove")
        await interaction.response.send_message(f"You no longer have {role.mention}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RoleManager(bot))
