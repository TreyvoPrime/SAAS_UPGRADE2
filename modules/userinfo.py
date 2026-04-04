import discord
from discord import app_commands
from discord.ext import commands


class UserInfo(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def format_dt(dt) -> str:
        if dt is None:
            return "Unknown"
        unix = int(dt.timestamp())
        return f"<t:{unix}:F>\n<t:{unix}:R>"

    @staticmethod
    def get_join_position(member: discord.Member) -> int | None:
        try:
            members = sorted(member.guild.members, key=lambda item: item.joined_at or discord.utils.utcnow())
        except Exception:
            return None
        for index, guild_member in enumerate(members, start=1):
            if guild_member.id == member.id:
                return index
        return None

    @staticmethod
    def get_badges(member: discord.Member) -> str:
        flags = member.public_flags
        badges = []
        if flags.staff:
            badges.append("Discord Staff")
        if flags.partner:
            badges.append("Partner")
        if flags.hypesquad:
            badges.append("HypeSquad Events")
        if flags.bug_hunter:
            badges.append("Bug Hunter")
        if getattr(flags, "bug_hunter_level_2", False):
            badges.append("Bug Hunter Level 2")
        if flags.verified_bot_developer:
            badges.append("Early Verified Bot Developer")
        if flags.early_supporter:
            badges.append("Early Supporter")
        if flags.discord_certified_moderator:
            badges.append("Certified Moderator")
        if flags.active_developer:
            badges.append("Active Developer")
        if flags.hypesquad_bravery:
            badges.append("HypeSquad Bravery")
        if flags.hypesquad_brilliance:
            badges.append("HypeSquad Brilliance")
        if flags.hypesquad_balance:
            badges.append("HypeSquad Balance")
        return ", ".join(badges) if badges else "None"

    @staticmethod
    def get_key_permissions(member: discord.Member) -> str:
        perms = member.guild_permissions
        important = []
        if perms.administrator:
            important.append("Administrator")
        if perms.manage_guild:
            important.append("Manage Server")
        if perms.manage_roles:
            important.append("Manage Roles")
        if perms.manage_channels:
            important.append("Manage Channels")
        if perms.manage_messages:
            important.append("Manage Messages")
        if perms.kick_members:
            important.append("Kick Members")
        if perms.ban_members:
            important.append("Ban Members")
        if perms.moderate_members:
            important.append("Timeout Members")
        if perms.mention_everyone:
            important.append("Mention Everyone")
        if perms.manage_webhooks:
            important.append("Manage Webhooks")
        return ", ".join(important) if important else "No major permissions"

    @staticmethod
    def get_status_text(member: discord.Member) -> str:
        status_map = {
            discord.Status.online: "Online",
            discord.Status.idle: "Idle",
            discord.Status.dnd: "Do Not Disturb",
            discord.Status.offline: "Offline",
            discord.Status.invisible: "Invisible",
        }
        return status_map.get(member.status, "Unknown")

    @staticmethod
    def get_activity_text(member: discord.Member) -> str:
        if not member.activities:
            return "None"

        activity_lines = []
        for activity in member.activities[:3]:
            activity_type = str(activity.type).split(".")[-1].replace("_", " ").title() if getattr(activity, "type", None) else "Activity"
            pieces = [activity_type]
            for value in (getattr(activity, "name", None), getattr(activity, "details", None), getattr(activity, "state", None)):
                if value:
                    pieces.append(str(value))
            activity_lines.append(" - ".join(pieces))
        return "\n".join(activity_lines) if activity_lines else "None"

    @staticmethod
    def get_voice_text(member: discord.Member) -> str:
        if not member.voice or not member.voice.channel:
            return "Not connected"

        voice = member.voice
        parts = [f"Channel: {voice.channel.mention}"]
        if voice.self_mute:
            parts.append("Self muted")
        if voice.self_deaf:
            parts.append("Self deafened")
        if voice.mute:
            parts.append("Server muted")
        if voice.deaf:
            parts.append("Server deafened")
        if voice.self_stream:
            parts.append("Streaming")
        if voice.self_video:
            parts.append("Camera on")
        if getattr(voice, "suppress", False):
            parts.append("Suppressed")
        return "\n".join(parts)

    @app_commands.command(name="userinfo", description="Show a clear profile summary for a member in this server")
    @app_commands.describe(member="The member you want to look up")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        target = member or interaction.user
        if not isinstance(target, discord.Member):
            await interaction.response.send_message("I couldn't load that member right now.", ephemeral=True)
            return

        roles = [role.mention for role in reversed(target.roles) if role.name != "@everyone"]
        role_count = len(roles)
        role_text = ", ".join(roles[:12]) if roles else "None"
        if role_count > 12:
            role_text += f"\n...and {role_count - 12} more"

        join_position = self.get_join_position(target)
        join_position_text = f"#{join_position}" if join_position else "Unknown"
        owner_text = "Yes" if target.guild.owner_id == target.id else "No"
        timeout_until = getattr(target, "timed_out_until", None)
        timeout_text = self.format_dt(timeout_until) if timeout_until else "Not timed out"
        premium_since = getattr(target, "premium_since", None)
        boost_text = self.format_dt(premium_since) if premium_since else "Not boosting"

        fetched_user = None
        try:
            fetched_user = await self.bot.fetch_user(target.id)
        except Exception:
            fetched_user = None

        banner_url = None
        accent_value = "Unknown"
        if fetched_user is not None:
            if fetched_user.banner:
                banner_url = fetched_user.banner.url
            accent = getattr(fetched_user, "accent_color", None) or getattr(fetched_user, "accent_colour", None)
            if accent:
                accent_value = str(accent)

        avatar_url = target.display_avatar.url
        guild_avatar = getattr(target, "guild_avatar", None)
        guild_avatar_text = guild_avatar.url if guild_avatar else "No server-specific avatar"
        embed_color = target.color if target.color != discord.Color.default() else discord.Color.blurple()

        embed = discord.Embed(title=f"User info for {target}", color=embed_color)
        embed.set_thumbnail(url=avatar_url)
        embed.add_field(name="Display name", value=target.display_name, inline=True)
        embed.add_field(name="Username", value=str(target), inline=True)
        embed.add_field(name="User ID", value=str(target.id), inline=True)
        embed.add_field(name="Bot", value="Yes" if target.bot else "No", inline=True)
        embed.add_field(name="Server owner", value=owner_text, inline=True)
        embed.add_field(name="Status", value=self.get_status_text(target), inline=True)
        embed.add_field(name="Account created", value=self.format_dt(target.created_at), inline=False)
        embed.add_field(name="Joined server", value=self.format_dt(target.joined_at), inline=False)
        embed.add_field(name="Join position", value=join_position_text, inline=True)
        embed.add_field(name="Top role", value=target.top_role.mention, inline=True)
        embed.add_field(name="Role count", value=str(role_count), inline=True)
        embed.add_field(name="Boosting since", value=boost_text, inline=False)
        embed.add_field(name="Timeout status", value=timeout_text, inline=False)
        embed.add_field(name="Key permissions", value=self.get_key_permissions(target), inline=False)
        embed.add_field(name="Badges", value=self.get_badges(target), inline=False)
        embed.add_field(name="Voice status", value=self.get_voice_text(target), inline=False)
        embed.add_field(name="Activities", value=self.get_activity_text(target), inline=False)
        embed.add_field(name=f"Roles [{role_count}]", value=role_text, inline=False)
        embed.add_field(
            name="Avatar links",
            value=f"[Global Avatar]({avatar_url})\n[Server Avatar]({guild_avatar_text})" if guild_avatar else f"[Global Avatar]({avatar_url})",
            inline=False,
        )
        embed.add_field(name="Accent color", value=accent_value, inline=True)
        if banner_url:
            embed.set_image(url=banner_url)
        embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(UserInfo(bot))
