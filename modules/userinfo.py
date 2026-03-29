import discord
from discord.ext import commands
from discord import app_commands


class UserInfo(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def format_dt(self, dt) -> str:
        if dt is None:
            return "Unknown"
        unix = int(dt.timestamp())
        return f"<t:{unix}:F>\n<t:{unix}:R>"

    def get_join_position(self, member: discord.Member) -> int | None:
        try:
            members = sorted(
                member.guild.members,
                key=lambda m: m.joined_at or discord.utils.utcnow()
            )
            for index, guild_member in enumerate(members, start=1):
                if guild_member.id == member.id:
                    return index
        except Exception:
            return None
        return None

    def get_badges(self, member: discord.Member) -> str:
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
            badges.append("Early Verified Bot Dev")
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

    def get_key_permissions(self, member: discord.Member) -> str:
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

    def get_status_text(self, member: discord.Member) -> str:
        status_map = {
            discord.Status.online: "Online",
            discord.Status.idle: "Idle",
            discord.Status.dnd: "Do Not Disturb",
            discord.Status.offline: "Offline",
            discord.Status.invisible: "Invisible",
        }
        return status_map.get(member.status, "Unknown")

    def get_activity_text(self, member: discord.Member) -> str:
        if not member.activities:
            return "None"

        activity_lines = []
        for activity in member.activities[:3]:
            name = getattr(activity, "name", None)
            details = getattr(activity, "details", None)
            state = getattr(activity, "state", None)
            activity_type = str(activity.type).split(".")[-1].title() if getattr(activity, "type", None) else "Activity"

            parts = [activity_type]
            if name:
                parts.append(name)
            if details:
                parts.append(details)
            if state:
                parts.append(state)

            activity_lines.append(" — ".join(parts))

        return "\n".join(activity_lines) if activity_lines else "None"

    def get_voice_text(self, member: discord.Member) -> str:
        if not member.voice or not member.voice.channel:
            return "Not connected"

        voice = member.voice
        parts = [f"Channel: {voice.channel.mention}"]

        if voice.self_mute:
            parts.append("Self Muted")
        if voice.self_deaf:
            parts.append("Self Deafened")
        if voice.mute:
            parts.append("Server Muted")
        if voice.deaf:
            parts.append("Server Deafened")
        if voice.self_stream:
            parts.append("Streaming")
        if voice.self_video:
            parts.append("Camera On")
        if getattr(voice, "suppress", False):
            parts.append("Suppressed")

        return "\n".join(parts)

    @app_commands.command(
        name="userinfo",
        description="Show detailed information about a user in this server"
    )
    @app_commands.describe(member="The member you want information about")
    async def userinfo(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None
    ):
        member = member or interaction.user

        # Role display (skip @everyone)
        roles = [role.mention for role in reversed(member.roles) if role.name != "@everyone"]
        role_count = len(roles)

        if roles:
            shown_roles = roles[:12]
            role_text = ", ".join(shown_roles)
            if role_count > 12:
                role_text += f"\n...and {role_count - 12} more"
        else:
            role_text = "None"

        # Join position
        join_position = self.get_join_position(member)
        join_position_text = f"#{join_position}" if join_position else "Unknown"

        # Owner check
        owner_text = "Yes" if member.guild.owner_id == member.id else "No"

        # Timeout status
        timed_out_until = getattr(member, "timed_out_until", None)
        if timed_out_until:
            timeout_text = self.format_dt(timed_out_until)
        else:
            timeout_text = "Not timed out"

        # Boost status
        premium_since = getattr(member, "premium_since", None)
        boost_text = self.format_dt(premium_since) if premium_since else "Not boosting"

        # Banner / accent
        # Fetching full user can expose banner when available
        fetched_user = None
        try:
            fetched_user = await self.bot.fetch_user(member.id)
        except Exception:
            fetched_user = None

        banner_url = None
        accent_value = "Unknown"

        if fetched_user:
            if fetched_user.banner:
                banner_url = fetched_user.banner.url
            accent = getattr(fetched_user, "accent_color", None) or getattr(fetched_user, "accent_colour", None)
            if accent:
                accent_value = str(accent)

        # Avatar info
        avatar_url = member.display_avatar.url
        guild_avatar = getattr(member, "guild_avatar", None)
        guild_avatar_text = guild_avatar.url if guild_avatar else "No server-specific avatar"

        embed_color = member.color if member.color != discord.Color.default() else discord.Color.blurple()

        embed = discord.Embed(
            title=f"User Info — {member}",
            color=embed_color
        )

        embed.set_thumbnail(url=avatar_url)

        embed.add_field(name="Display Name", value=member.display_name, inline=True)
        embed.add_field(name="Username", value=str(member), inline=True)
        embed.add_field(name="User ID", value=str(member.id), inline=True)

        embed.add_field(name="Bot", value="Yes" if member.bot else "No", inline=True)
        embed.add_field(name="Server Owner", value=owner_text, inline=True)
        embed.add_field(name="Status", value=self.get_status_text(member), inline=True)

        embed.add_field(name="Account Created", value=self.format_dt(member.created_at), inline=False)
        embed.add_field(name="Joined Server", value=self.format_dt(member.joined_at), inline=False)
        embed.add_field(name="Join Position", value=join_position_text, inline=True)

        embed.add_field(name="Top Role", value=member.top_role.mention, inline=True)
        embed.add_field(name="Role Count", value=str(role_count), inline=True)
        embed.add_field(name="Boosting Since", value=boost_text, inline=False)

        embed.add_field(name="Timeout Status", value=timeout_text, inline=False)
        embed.add_field(name="Key Permissions", value=self.get_key_permissions(member), inline=False)
        embed.add_field(name="Badges", value=self.get_badges(member), inline=False)

        embed.add_field(name="Voice Status", value=self.get_voice_text(member), inline=False)
        embed.add_field(name="Activities", value=self.get_activity_text(member), inline=False)
        embed.add_field(name=f"Roles [{role_count}]", value=role_text, inline=False)

        embed.add_field(
            name="Avatar Links",
            value=f"[Global Avatar]({avatar_url})\n[Server Avatar]({guild_avatar_text})" if guild_avatar else f"[Global Avatar]({avatar_url})",
            inline=False
        )

        embed.add_field(name="Accent Color", value=accent_value, inline=True)

        if banner_url:
            embed.set_image(url=banner_url)

        embed.set_footer(
            text=f"Requested by {interaction.user}",
            icon_url=interaction.user.display_avatar.url
        )

        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(UserInfo(bot))