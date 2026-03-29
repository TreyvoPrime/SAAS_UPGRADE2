import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands

DATA_FILE = Path("auditlog_config.json")
AUDIT_ROLE_NAME = "AuditLog"


def load_config() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_config(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def truncate(text: Optional[str], limit: int = 1000) -> str:
    if text is None:
        return "None"
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def fmt_user(user: Optional[discord.abc.User]) -> str:
    if user is None:
        return "Unknown"
    if hasattr(user, "mention"):
        return f"{user.mention} (`{user}`)"
    return f"`{user}`"


def fmt_channel(channel) -> str:
    if channel is None:
        return "Unknown"
    try:
        return channel.mention
    except Exception:
        return f"`{channel}`"


def safe_jump_url(message: discord.Message) -> str:
    try:
        return message.jump_url
    except Exception:
        return ""


async def ensure_audit_role(
    guild: discord.Guild,
    bot_member: discord.Member
) -> discord.Role:
    role = discord.utils.get(guild.roles, name=AUDIT_ROLE_NAME)
    if role:
        return role

    role = await guild.create_role(
        name=AUDIT_ROLE_NAME,
        reason="Auto-created AuditLog role"
    )

    try:
        if role < bot_member.top_role:
            await guild.edit_role_positions(
                positions={role: bot_member.top_role.position - 1}
            )
    except discord.HTTPException:
        pass

    return role


class AuditLogCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = load_config()

    # ----------------------------
    # CONFIG HELPERS
    # ----------------------------
    def get_guild_config(self, guild_id: int) -> dict:
        return self.config.get(str(guild_id), {})

    def set_log_channel(self, guild_id: int, channel_id: int) -> None:
        self.config[str(guild_id)] = {"channel_id": channel_id}
        save_config(self.config)

    def remove_log_channel(self, guild_id: int) -> bool:
        guild_id_str = str(guild_id)
        if guild_id_str in self.config:
            del self.config[guild_id_str]
            save_config(self.config)
            return True
        return False

    def get_log_channel_id(self, guild_id: int) -> Optional[int]:
        return self.get_guild_config(guild_id).get("channel_id")

    def get_log_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        channel_id = self.get_log_channel_id(guild.id)
        if not channel_id:
            return None

        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    def user_has_audit_role(self, member: discord.Member) -> bool:
        return any(role.name == AUDIT_ROLE_NAME for role in member.roles)

    async def send_log(self, guild: discord.Guild, embed: discord.Embed) -> None:
        channel = self.get_log_channel(guild)
        if channel is None:
            return

        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    def make_embed(self, title: str, color: discord.Color) -> discord.Embed:
        return discord.Embed(
            title=title,
            color=color,
            timestamp=datetime.now(timezone.utc)
        )

    # ----------------------------
    # COMMANDS
    # ----------------------------
    @app_commands.command(name="setauditlog", description="Set the channel used for logging")
    @app_commands.describe(channel="The channel where server logs should be sent")
    async def setauditlog(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command only works in a server.",
                ephemeral=True
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Could not verify your member info.",
                ephemeral=True
            )
            return

        bot_member = interaction.guild.me
        if bot_member is None:
            await interaction.response.send_message(
                "Could not verify bot permissions.",
                ephemeral=True
            )
            return

        if not bot_member.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "I need **Manage Roles** to create the AuditLog role.",
                ephemeral=True
            )
            return

        await ensure_audit_role(interaction.guild, bot_member)

        if not (
            interaction.user.guild_permissions.administrator
            or self.user_has_audit_role(interaction.user)
        ):
            await interaction.response.send_message(
                f"You need the **{AUDIT_ROLE_NAME}** role or Administrator to use this.",
                ephemeral=True
            )
            return

        self.set_log_channel(interaction.guild.id, channel.id)

        await interaction.response.send_message(
            f"Audit log channel set to {channel.mention}.",
            ephemeral=True
        )

        embed = self.make_embed("✅ Logging Enabled", discord.Color.green())
        embed.description = "This channel is now the active server log channel."
        await self.send_log(interaction.guild, embed)

    @app_commands.command(name="removeauditlog", description="Remove the configured audit log channel")
    async def removeauditlog(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command only works in a server.",
                ephemeral=True
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Could not verify your member info.",
                ephemeral=True
            )
            return

        if not (
            interaction.user.guild_permissions.administrator
            or self.user_has_audit_role(interaction.user)
        ):
            await interaction.response.send_message(
                f"You need the **{AUDIT_ROLE_NAME}** role or Administrator to use this.",
                ephemeral=True
            )
            return

        removed = self.remove_log_channel(interaction.guild.id)

        if not removed:
            await interaction.response.send_message(
                "No audit log channel is currently configured.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Audit log channel removed.",
            ephemeral=True
        )

    @app_commands.command(name="giveauditrole", description="Give the AuditLog role to a member")
    @app_commands.describe(member="The member to receive the AuditLog role")
    async def giveauditrole(self, interaction: discord.Interaction, member: discord.Member):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command only works in a server.",
                ephemeral=True
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Could not verify your member info.",
                ephemeral=True
            )
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only administrators can use this command.",
                ephemeral=True
            )
            return

        bot_member = interaction.guild.me
        if bot_member is None:
            await interaction.response.send_message(
                "Could not verify bot permissions.",
                ephemeral=True
            )
            return

        if not bot_member.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "I need **Manage Roles** to assign the AuditLog role.",
                ephemeral=True
            )
            return

        role = await ensure_audit_role(interaction.guild, bot_member)

        if role >= bot_member.top_role:
            await interaction.response.send_message(
                "I can't assign the AuditLog role because it is above my highest role.",
                ephemeral=True
            )
            return

        try:
            await member.add_roles(role, reason="Granted AuditLog role")
            await interaction.response.send_message(
                f"Gave **{AUDIT_ROLE_NAME}** to {member.mention}.",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "Discord blocked me from assigning that role.",
                ephemeral=True
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"Failed to assign role: `{e}`",
                ephemeral=True
            )

    # ----------------------------
    # MEMBER EVENTS
    # ----------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        embed = self.make_embed("📥 Member Joined", discord.Color.green())
        embed.description = f"{member.mention} joined the server."
        embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=False)

        try:
            embed.set_thumbnail(url=member.display_avatar.url)
        except Exception:
            pass

        if member.created_at:
            embed.add_field(
                name="Account Created",
                value=discord.utils.format_dt(member.created_at, style="F"),
                inline=False
            )

        await self.send_log(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        embed = self.make_embed("📤 Member Left", discord.Color.red())
        embed.description = f"{member} left or was removed from the server."
        embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=False)

        try:
            embed.set_thumbnail(url=member.display_avatar.url)
        except Exception:
            pass

        await self.send_log(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        embed = self.make_embed("🔨 Member Banned", discord.Color.red())
        embed.description = f"{fmt_user(user)} was banned."
        embed.add_field(name="User ID", value=str(user.id), inline=False)
        await self.send_log(guild, embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        embed = self.make_embed("♻️ Member Unbanned", discord.Color.green())
        embed.description = f"{fmt_user(user)} was unbanned."
        embed.add_field(name="User ID", value=str(user.id), inline=False)
        await self.send_log(guild, embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.nick != after.nick:
            embed = self.make_embed("📝 Nickname Updated", discord.Color.blurple())
            embed.description = f"{after.mention}'s nickname changed."
            embed.add_field(name="Before", value=truncate(before.nick or before.name), inline=True)
            embed.add_field(name="After", value=truncate(after.nick or after.name), inline=True)
            await self.send_log(after.guild, embed)

        before_roles = set(before.roles)
        after_roles = set(after.roles)

        added_roles = [role.mention for role in after_roles - before_roles if role.name != "@everyone"]
        removed_roles = [role.mention for role in before_roles - after_roles if role.name != "@everyone"]

        if added_roles or removed_roles:
            embed = self.make_embed("🎭 Member Roles Updated", discord.Color.gold())
            embed.description = f"{after.mention}'s roles changed."

            if added_roles:
                embed.add_field(
                    name="Added Roles",
                    value=truncate(", ".join(added_roles), 1024),
                    inline=False
                )

            if removed_roles:
                embed.add_field(
                    name="Removed Roles",
                    value=truncate(", ".join(removed_roles), 1024),
                    inline=False
                )

            await self.send_log(after.guild, embed)

    # ----------------------------
    # MESSAGE EVENTS
    # ----------------------------
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return

        embed = self.make_embed("🗑️ Message Deleted", discord.Color.red())
        embed.add_field(
            name="Author",
            value=f"{message.author.mention} (`{message.author.id}`)",
            inline=False
        )
        embed.add_field(name="Channel", value=message.channel.mention, inline=False)

        content = message.content if message.content else "*No text content*"
        embed.add_field(name="Content", value=truncate(content, 1000), inline=False)

        if message.attachments:
            files = ", ".join(a.filename for a in message.attachments)
            embed.add_field(name="Attachments", value=truncate(files, 1000), inline=False)

        await self.send_log(message.guild, embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.guild is None or before.author.bot:
            return

        if before.content == after.content:
            return

        embed = self.make_embed("✏️ Message Edited", discord.Color.orange())
        embed.add_field(
            name="Author",
            value=f"{before.author.mention} (`{before.author.id}`)",
            inline=False
        )
        embed.add_field(name="Channel", value=before.channel.mention, inline=False)
        embed.add_field(
            name="Before",
            value=truncate(before.content or "*No text content*", 1000),
            inline=False
        )
        embed.add_field(
            name="After",
            value=truncate(after.content or "*No text content*", 1000),
            inline=False
        )

        jump = safe_jump_url(after)
        if jump:
            embed.add_field(name="Jump", value=f"[Go to Message]({jump})", inline=False)

        await self.send_log(before.guild, embed)

    # ----------------------------
    # VOICE EVENTS
    # ----------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ):
        guild = member.guild

        if before.channel is None and after.channel is not None:
            embed = self.make_embed("🔊 Voice Join", discord.Color.green())
            embed.description = f"{member.mention} joined {after.channel.mention}."
            await self.send_log(guild, embed)
            return

        if before.channel is not None and after.channel is None:
            embed = self.make_embed("🔇 Voice Leave", discord.Color.red())
            embed.description = f"{member.mention} left {before.channel.mention}."
            await self.send_log(guild, embed)
            return

        if before.channel != after.channel and before.channel is not None and after.channel is not None:
            embed = self.make_embed("🔁 Voice Move", discord.Color.blurple())
            embed.description = (
                f"{member.mention} moved from {before.channel.mention} "
                f"to {after.channel.mention}."
            )
            await self.send_log(guild, embed)

        changes = []

        if before.self_mute != after.self_mute:
            changes.append(f"Self Mute: `{before.self_mute}` → `{after.self_mute}`")
        if before.self_deaf != after.self_deaf:
            changes.append(f"Self Deaf: `{before.self_deaf}` → `{after.self_deaf}`")
        if before.mute != after.mute:
            changes.append(f"Server Mute: `{before.mute}` → `{after.mute}`")
        if before.deaf != after.deaf:
            changes.append(f"Server Deaf: `{before.deaf}` → `{after.deaf}`")
        if before.self_stream != after.self_stream:
            changes.append(f"Streaming: `{before.self_stream}` → `{after.self_stream}`")
        if before.self_video != after.self_video:
            changes.append(f"Camera: `{before.self_video}` → `{after.self_video}`")

        if changes:
            embed = self.make_embed("🎙️ Voice State Updated", discord.Color.gold())
            embed.description = f"{member.mention} updated voice state."
            embed.add_field(name="Changes", value=truncate("\n".join(changes), 1024), inline=False)
            await self.send_log(guild, embed)

    # ----------------------------
    # CHANNEL EVENTS
    # ----------------------------
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        embed = self.make_embed("📁 Channel Created", discord.Color.green())
        embed.description = f"{fmt_channel(channel)} was created."
        await self.send_log(channel.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        embed = self.make_embed("🗑️ Channel Deleted", discord.Color.red())
        embed.description = f"`{channel.name}` was deleted."
        await self.send_log(channel.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        changes = []

        if before.name != after.name:
            changes.append(f"Name: `{before.name}` → `{after.name}`")

        if hasattr(before, "topic") and hasattr(after, "topic"):
            if before.topic != after.topic:
                changes.append("Topic changed")

        if changes:
            embed = self.make_embed("🛠️ Channel Updated", discord.Color.orange())
            embed.description = f"{fmt_channel(after)} was updated."
            embed.add_field(name="Changes", value=truncate("\n".join(changes), 1024), inline=False)
            await self.send_log(after.guild, embed)

    # ----------------------------
    # ROLE EVENTS
    # ----------------------------
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        if role.name == AUDIT_ROLE_NAME:
            return

        embed = self.make_embed("➕ Role Created", discord.Color.green())
        embed.description = f"Role {role.mention} was created."
        await self.send_log(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        if role.name == AUDIT_ROLE_NAME:
            return

        embed = self.make_embed("➖ Role Deleted", discord.Color.red())
        embed.description = f"Role `{role.name}` was deleted."
        await self.send_log(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        changes = []

        if before.name != after.name:
            changes.append(f"Name: `{before.name}` → `{after.name}`")
        if before.color != after.color:
            changes.append(f"Color: `{before.color}` → `{after.color}`")
        if before.permissions != after.permissions:
            changes.append("Permissions changed")

        if changes:
            embed = self.make_embed("🧩 Role Updated", discord.Color.orange())
            embed.description = f"Role {after.mention} was updated."
            embed.add_field(name="Changes", value=truncate("\n".join(changes), 1024), inline=False)
            await self.send_log(after.guild, embed)

    # ----------------------------
    # AUDIT LOG ENTRY EVENT
    # ----------------------------
    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        guild = entry.guild

        action = str(entry.action).replace("AuditLogAction.", "")
        user = fmt_user(entry.user)
        target = "Unknown"

        if entry.target is not None:
            try:
                target = getattr(entry.target, "mention", None) or f"`{entry.target}`"
            except Exception:
                target = f"`{entry.target}`"

        embed = self.make_embed("📜 Audit Log Entry", discord.Color.dark_orange())
        embed.add_field(name="Action", value=f"`{action}`", inline=False)
        embed.add_field(name="User", value=user, inline=False)
        embed.add_field(name="Target", value=target, inline=False)

        if entry.reason:
            embed.add_field(name="Reason", value=truncate(entry.reason, 1000), inline=False)

        if entry.extra is not None:
            embed.add_field(name="Extra", value=truncate(repr(entry.extra), 1000), inline=False)

        await self.send_log(guild, embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(AuditLogCog(bot))