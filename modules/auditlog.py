from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands


DATA_FILE = Path("auditlog_config.json")
AUDIT_ROLE_NAME = "AuditLog"
MAX_FIELD = 1000


def load_config() -> dict[str, dict[str, Any]]:
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def save_config(data: dict[str, dict[str, Any]]) -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def truncate(text: Any, limit: int = MAX_FIELD) -> str:
    value = "None" if text is None else str(text)
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def fmt_user(user: discord.abc.User | None) -> str:
    if user is None:
        return "Unknown"
    mention = getattr(user, "mention", None)
    return f"{mention} (`{user}`)" if mention else f"`{user}`"


def fmt_channel(channel: discord.abc.GuildChannel | discord.Thread | None) -> str:
    if channel is None:
        return "Unknown"
    mention = getattr(channel, "mention", None)
    return mention or f"`{getattr(channel, 'name', channel)}`"


def fmt_roles(roles: list[discord.Role]) -> str:
    if not roles:
        return "None"
    return truncate(", ".join(role.mention for role in roles), 1024)


def safe_jump_url(message: discord.Message) -> str:
    try:
        return message.jump_url
    except Exception:
        return ""


async def ensure_audit_role(guild: discord.Guild, bot_member: discord.Member) -> discord.Role:
    role = discord.utils.get(guild.roles, name=AUDIT_ROLE_NAME)
    if role:
        return role

    role = await guild.create_role(name=AUDIT_ROLE_NAME, reason="Auto-created AuditLog role")
    try:
        if role < bot_member.top_role:
            await guild.edit_role_positions(positions={role: bot_member.top_role.position - 1})
    except discord.HTTPException:
        pass
    return role


class AuditLogCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = load_config()

    def get_guild_config(self, guild_id: int) -> dict[str, Any]:
        return self.config.get(str(guild_id), {})

    def set_log_channel(self, guild_id: int, channel_id: int) -> None:
        self.config[str(guild_id)] = {"channel_id": int(channel_id)}
        save_config(self.config)

    def remove_log_channel(self, guild_id: int) -> bool:
        guild_key = str(guild_id)
        if guild_key not in self.config:
            return False
        del self.config[guild_key]
        save_config(self.config)
        return True

    def get_log_channel_id(self, guild_id: int) -> int | None:
        value = self.get_guild_config(guild_id).get("channel_id")
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None

    def get_log_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel_id = self.get_log_channel_id(guild.id)
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        return channel if isinstance(channel, discord.TextChannel) else None

    def user_has_audit_role(self, member: discord.Member) -> bool:
        return any(role.name == AUDIT_ROLE_NAME for role in member.roles)

    def make_embed(self, title: str, color: discord.Color, description: str | None = None) -> discord.Embed:
        embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
        if description:
            embed.description = description
        return embed

    def _append_dashboard_entry(
        self,
        guild: discord.Guild,
        *,
        title: str,
        summary: str,
        status: str = "event",
        user_name: str | None = None,
        channel_name: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not hasattr(self.bot, "command_logs"):
            return
        payload = {
            "guild_id": guild.id,
            "guild_name": guild.name,
            "kind": "event",
            "title": title,
            "summary": summary,
            "status": status,
            "user_name": user_name or "Server activity",
            "channel_name": channel_name,
        }
        if extra:
            payload.update(extra)
        self.bot.command_logs.append(payload)

    async def emit_event(
        self,
        guild: discord.Guild,
        *,
        title: str,
        description: str,
        color: discord.Color,
        status: str = "event",
        user_name: str | None = None,
        channel_name: str | None = None,
        fields: list[tuple[str, str, bool]] | None = None,
        thumbnail_url: str | None = None,
    ) -> None:
        embed = self.make_embed(title, color, description)
        for name, value, inline in fields or []:
            embed.add_field(name=name, value=truncate(value, 1024), inline=inline)
        if thumbnail_url:
            try:
                embed.set_thumbnail(url=thumbnail_url)
            except Exception:
                pass

        channel = self.get_log_channel(guild)
        if channel is not None:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass

        summary = description
        if fields:
            summary = f"{description} | " + " | ".join(f"{name}: {value}" for name, value, _ in fields[:2])
        self._append_dashboard_entry(
            guild,
            title=title,
            summary=summary,
            status=status,
            user_name=user_name,
            channel_name=channel_name,
        )

    async def emit_external_event(
        self,
        guild_id: int,
        *,
        title: str,
        description: str,
        status: str = "event",
        color: discord.Color | None = None,
        user_name: str | None = None,
        channel_name: str | None = None,
        fields: list[tuple[str, str, bool]] | None = None,
    ) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        await self.emit_event(
            guild,
            title=title,
            description=description,
            color=color or discord.Color.blurple(),
            status=status,
            user_name=user_name,
            channel_name=channel_name,
            fields=fields,
        )

    @app_commands.command(name="setauditlog", description="Choose the channel where server events should be logged")
    async def setauditlog(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        bot_member = interaction.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            await interaction.response.send_message("I need Manage Roles to create the AuditLog role.", ephemeral=True)
            return

        await ensure_audit_role(interaction.guild, bot_member)
        if not (interaction.user.guild_permissions.administrator or self.user_has_audit_role(interaction.user)):
            await interaction.response.send_message(
                f"You need the {AUDIT_ROLE_NAME} role or Administrator to use this.",
                ephemeral=True,
            )
            return

        self.set_log_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(f"Server event logs will go to {channel.mention}.", ephemeral=True)
        await self.emit_event(
            interaction.guild,
            title="Logging Enabled",
            description=f"Server logging now points to {channel.mention}.",
            color=discord.Color.green(),
            status="event",
            user_name=str(interaction.user),
            channel_name=channel.name,
        )

    @app_commands.command(name="removeauditlog", description="Stop sending server event logs to the configured channel")
    async def removeauditlog(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        if not (interaction.user.guild_permissions.administrator or self.user_has_audit_role(interaction.user)):
            await interaction.response.send_message(
                f"You need the {AUDIT_ROLE_NAME} role or Administrator to use this.",
                ephemeral=True,
            )
            return
        if not self.remove_log_channel(interaction.guild.id):
            await interaction.response.send_message("No server log channel is configured right now.", ephemeral=True)
            return
        await interaction.response.send_message("Server logging disabled.", ephemeral=True)
        self._append_dashboard_entry(
            interaction.guild,
            title="Logging Disabled",
            summary=f"{interaction.user} removed the configured audit log channel.",
            status="event",
            user_name=str(interaction.user),
        )

    @app_commands.command(name="giveauditrole", description="Give the AuditLog role to a staff member")
    async def giveauditrole(self, interaction: discord.Interaction, member: discord.Member):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can use this command.", ephemeral=True)
            return

        bot_member = interaction.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            await interaction.response.send_message("I need Manage Roles to assign the AuditLog role.", ephemeral=True)
            return

        role = await ensure_audit_role(interaction.guild, bot_member)
        if role >= bot_member.top_role:
            await interaction.response.send_message("I can't assign that role because it sits above my highest role.", ephemeral=True)
            return

        try:
            await member.add_roles(role, reason="Granted AuditLog role")
        except discord.Forbidden:
            await interaction.response.send_message("Discord blocked me from assigning that role.", ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.response.send_message("I couldn't assign that role right now.", ephemeral=True)
            return

        await interaction.response.send_message(f"Gave {AUDIT_ROLE_NAME} to {member.mention}.", ephemeral=True)
        await self.emit_event(
            interaction.guild,
            title="Audit Role Granted",
            description=f"{member.mention} can now manage event logging.",
            color=discord.Color.blurple(),
            status="event",
            user_name=str(interaction.user),
            fields=[("Granted To", str(member), False)],
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        fields = [
            ("User", f"{member} (`{member.id}`)", False),
        ]
        if member.created_at:
            fields.append(("Account Created", discord.utils.format_dt(member.created_at, style="F"), False))
        await self.emit_event(
            member.guild,
            title="Member Joined",
            description=f"{member.mention} joined the server.",
            color=discord.Color.green(),
            status="event",
            user_name=str(member),
            fields=fields,
            thumbnail_url=getattr(member.display_avatar, "url", None),
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self.emit_event(
            member.guild,
            title="Member Left",
            description=f"{member} left or was removed from the server.",
            color=discord.Color.red(),
            status="event",
            user_name=str(member),
            fields=[("User", f"{member} (`{member.id}`)", False)],
            thumbnail_url=getattr(member.display_avatar, "url", None),
        )

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        await self.emit_event(
            guild,
            title="Member Banned",
            description=f"{fmt_user(user)} was banned.",
            color=discord.Color.red(),
            status="event",
            user_name=str(user),
            fields=[("User ID", str(user.id), False)],
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        await self.emit_event(
            guild,
            title="Member Unbanned",
            description=f"{fmt_user(user)} was unbanned.",
            color=discord.Color.green(),
            status="event",
            user_name=str(user),
            fields=[("User ID", str(user.id), False)],
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.nick != after.nick:
            await self.emit_event(
                after.guild,
                title="Nickname Updated",
                description=f"{after.mention}'s nickname changed.",
                color=discord.Color.blurple(),
                status="event",
                user_name=str(after),
                fields=[
                    ("Before", before.nick or before.name, True),
                    ("After", after.nick or after.name, True),
                ],
                thumbnail_url=getattr(after.display_avatar, "url", None),
            )

        added_roles = [role for role in after.roles if role not in before.roles and not role.is_default()]
        removed_roles = [role for role in before.roles if role not in after.roles and not role.is_default()]
        if added_roles or removed_roles:
            fields: list[tuple[str, str, bool]] = []
            if added_roles:
                fields.append(("Added Roles", fmt_roles(added_roles), False))
            if removed_roles:
                fields.append(("Removed Roles", fmt_roles(removed_roles), False))
            await self.emit_event(
                after.guild,
                title="Member Roles Updated",
                description=f"{after.mention}'s roles changed.",
                color=discord.Color.gold(),
                status="event",
                user_name=str(after),
                fields=fields,
                thumbnail_url=getattr(after.display_avatar, "url", None),
            )

        if before.communication_disabled_until != after.communication_disabled_until:
            await self.emit_event(
                after.guild,
                title="Timeout Updated",
                description=f"{after.mention}'s timeout changed.",
                color=discord.Color.orange(),
                status="event",
                user_name=str(after),
                fields=[
                    ("Before", str(before.communication_disabled_until or "No timeout"), False),
                    ("After", str(after.communication_disabled_until or "No timeout"), False),
                ],
            )

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        fields = [
            ("Author", f"{message.author.mention} (`{message.author.id}`)", False),
            ("Channel", fmt_channel(message.channel), False),
            ("Content", message.content or "*No text content*", False),
        ]
        if message.attachments:
            fields.append(("Attachments", ", ".join(a.filename for a in message.attachments), False))
        await self.emit_event(
            message.guild,
            title="Message Deleted",
            description="A message was deleted.",
            color=discord.Color.red(),
            status="event",
            user_name=str(message.author),
            channel_name=getattr(message.channel, "name", None),
            fields=fields,
        )

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]):
        if not messages:
            return
        first = next((message for message in messages if message.guild is not None), None)
        if first is None or first.guild is None:
            return
        authors = {str(message.author) for message in messages if getattr(message, "author", None) is not None}
        await self.emit_event(
            first.guild,
            title="Bulk Messages Deleted",
            description=f"{len(messages)} messages were deleted in bulk.",
            color=discord.Color.red(),
            status="event",
            channel_name=getattr(first.channel, "name", None),
            fields=[
                ("Channel", fmt_channel(first.channel), False),
                ("Authors Seen", ", ".join(sorted(authors)) or "Unknown", False),
            ],
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.guild is None or before.author.bot or before.content == after.content:
            return
        fields = [
            ("Author", f"{before.author.mention} (`{before.author.id}`)", False),
            ("Channel", fmt_channel(before.channel), False),
            ("Before", before.content or "*No text content*", False),
            ("After", after.content or "*No text content*", False),
        ]
        jump = safe_jump_url(after)
        if jump:
            fields.append(("Jump", f"[Go to Message]({jump})", False))
        await self.emit_event(
            before.guild,
            title="Message Edited",
            description="A message was edited.",
            color=discord.Color.orange(),
            status="event",
            user_name=str(before.author),
            channel_name=getattr(before.channel, "name", None),
            fields=fields,
        )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        if before.channel is None and after.channel is not None:
            await self.emit_event(
                guild,
                title="Voice Join",
                description=f"{member.mention} joined {after.channel.mention}.",
                color=discord.Color.green(),
                status="event",
                user_name=str(member),
                channel_name=after.channel.name,
            )
            return
        if before.channel is not None and after.channel is None:
            await self.emit_event(
                guild,
                title="Voice Leave",
                description=f"{member.mention} left {before.channel.mention}.",
                color=discord.Color.red(),
                status="event",
                user_name=str(member),
                channel_name=before.channel.name,
            )
            return
        if before.channel != after.channel and before.channel and after.channel:
            await self.emit_event(
                guild,
                title="Voice Move",
                description=f"{member.mention} moved from {before.channel.mention} to {after.channel.mention}.",
                color=discord.Color.blurple(),
                status="event",
                user_name=str(member),
                channel_name=after.channel.name,
            )
        changes = []
        if before.self_mute != after.self_mute:
            changes.append(f"Self Mute: {before.self_mute} -> {after.self_mute}")
        if before.self_deaf != after.self_deaf:
            changes.append(f"Self Deaf: {before.self_deaf} -> {after.self_deaf}")
        if before.mute != after.mute:
            changes.append(f"Server Mute: {before.mute} -> {after.mute}")
        if before.deaf != after.deaf:
            changes.append(f"Server Deaf: {before.deaf} -> {after.deaf}")
        if before.self_stream != after.self_stream:
            changes.append(f"Streaming: {before.self_stream} -> {after.self_stream}")
        if before.self_video != after.self_video:
            changes.append(f"Camera: {before.self_video} -> {after.self_video}")
        if changes:
            await self.emit_event(
                guild,
                title="Voice State Updated",
                description=f"{member.mention}'s voice state changed.",
                color=discord.Color.gold(),
                status="event",
                user_name=str(member),
                fields=[("Changes", "\n".join(changes), False)],
            )

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        await self.emit_event(
            channel.guild,
            title="Channel Created",
            description=f"{fmt_channel(channel)} was created.",
            color=discord.Color.green(),
            status="event",
            channel_name=getattr(channel, "name", None),
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        await self.emit_event(
            channel.guild,
            title="Channel Deleted",
            description=f"`{channel.name}` was deleted.",
            color=discord.Color.red(),
            status="event",
            channel_name=getattr(channel, "name", None),
        )

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        changes = []
        if before.name != after.name:
            changes.append(f"Name: {before.name} -> {after.name}")
        if hasattr(before, "topic") and hasattr(after, "topic") and before.topic != after.topic:
            changes.append("Topic changed")
        if getattr(before, "slowmode_delay", None) != getattr(after, "slowmode_delay", None):
            changes.append(f"Slowmode: {getattr(before, 'slowmode_delay', 0)} -> {getattr(after, 'slowmode_delay', 0)}")
        if changes:
            await self.emit_event(
                after.guild,
                title="Channel Updated",
                description=f"{fmt_channel(after)} was updated.",
                color=discord.Color.orange(),
                status="event",
                channel_name=getattr(after, "name", None),
                fields=[("Changes", "\n".join(changes), False)],
            )

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        await self.emit_event(
            thread.guild,
            title="Thread Created",
            description=f"{thread.mention} was created.",
            color=discord.Color.green(),
            status="event",
            channel_name=thread.name,
            fields=[("Parent Channel", fmt_channel(thread.parent), False)],
        )

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        await self.emit_event(
            thread.guild,
            title="Thread Deleted",
            description=f"`{thread.name}` was deleted.",
            color=discord.Color.red(),
            status="event",
            channel_name=thread.name,
        )

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        changes = []
        if before.name != after.name:
            changes.append(f"Name: {before.name} -> {after.name}")
        if before.archived != after.archived:
            changes.append(f"Archived: {before.archived} -> {after.archived}")
        if before.locked != after.locked:
            changes.append(f"Locked: {before.locked} -> {after.locked}")
        if changes:
            await self.emit_event(
                after.guild,
                title="Thread Updated",
                description=f"{after.mention} was updated.",
                color=discord.Color.orange(),
                status="event",
                channel_name=after.name,
                fields=[("Changes", "\n".join(changes), False)],
            )

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        if role.name == AUDIT_ROLE_NAME:
            return
        await self.emit_event(
            role.guild,
            title="Role Created",
            description=f"Role {role.mention} was created.",
            color=discord.Color.green(),
            status="event",
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        if role.name == AUDIT_ROLE_NAME:
            return
        await self.emit_event(
            role.guild,
            title="Role Deleted",
            description=f"Role `{role.name}` was deleted.",
            color=discord.Color.red(),
            status="event",
        )

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        changes = []
        if before.name != after.name:
            changes.append(f"Name: {before.name} -> {after.name}")
        if before.color != after.color:
            changes.append(f"Color: {before.color} -> {after.color}")
        if before.permissions != after.permissions:
            changes.append("Permissions changed")
        if changes:
            await self.emit_event(
                after.guild,
                title="Role Updated",
                description=f"Role {after.mention} was updated.",
                color=discord.Color.orange(),
                status="event",
                fields=[("Changes", "\n".join(changes), False)],
            )

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild is None:
            return
        await self.emit_event(
            invite.guild,
            title="Invite Created",
            description=f"Invite `{invite.code}` was created.",
            color=discord.Color.green(),
            status="event",
            user_name=str(invite.inviter) if invite.inviter else None,
            channel_name=getattr(invite.channel, "name", None),
            fields=[
                ("Channel", fmt_channel(invite.channel), False),
                ("Max Uses", str(invite.max_uses or "Unlimited"), True),
                ("Expires", str(invite.max_age or "Never"), True),
            ],
        )

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        if invite.guild is None:
            return
        await self.emit_event(
            invite.guild,
            title="Invite Deleted",
            description=f"Invite `{invite.code}` was deleted.",
            color=discord.Color.red(),
            status="event",
            channel_name=getattr(invite.channel, "name", None),
            fields=[("Channel", fmt_channel(invite.channel), False)],
        )

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        changes = []
        if before.name != after.name:
            changes.append(f"Name: {before.name} -> {after.name}")
        if before.verification_level != after.verification_level:
            changes.append(f"Verification: {before.verification_level} -> {after.verification_level}")
        if before.system_channel != after.system_channel:
            changes.append("System channel changed")
        if before.afk_channel != after.afk_channel:
            changes.append("AFK channel changed")
        if before.icon != after.icon:
            changes.append("Server icon changed")
        if changes:
            await self.emit_event(
                after,
                title="Server Updated",
                description="Server settings changed.",
                color=discord.Color.blurple(),
                status="event",
                fields=[("Changes", "\n".join(changes), False)],
            )

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before: tuple[discord.Emoji, ...], after: tuple[discord.Emoji, ...]):
        before_names = {emoji.name for emoji in before}
        after_names = {emoji.name for emoji in after}
        added = sorted(after_names - before_names)
        removed = sorted(before_names - after_names)
        if added or removed:
            fields = []
            if added:
                fields.append(("Added", ", ".join(added), False))
            if removed:
                fields.append(("Removed", ", ".join(removed), False))
            await self.emit_event(
                guild,
                title="Emoji List Updated",
                description="Server emojis changed.",
                color=discord.Color.gold(),
                status="event",
                fields=fields,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(AuditLogCog(bot))
