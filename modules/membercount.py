from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands


def _count_recent_members(guild: discord.Guild, *, days: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    return sum(1 for member in guild.members if member.joined_at and member.joined_at >= cutoff)


def _count_online_members(guild: discord.Guild) -> int:
    return sum(1 for member in guild.members if member.status != discord.Status.offline)


def _count_voice_members(guild: discord.Guild) -> int:
    return sum(1 for member in guild.members if member.voice and member.voice.channel)


def _top_roles(guild: discord.Guild, limit: int = 6) -> list[tuple[discord.Role, int]]:
    ranked = []
    for role in guild.roles:
        if role.is_default():
            continue
        member_count = len(role.members)
        if member_count <= 0:
            continue
        ranked.append((role, member_count))
    ranked.sort(key=lambda item: (item[1], item[0].position), reverse=True)
    return ranked[:limit]


class MemberCount(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _require_guild(self, interaction: discord.Interaction) -> discord.Guild | None:
        if interaction.guild is None:
            return None
        return interaction.guild

    def _build_overview_embed(self, guild: discord.Guild, requester: discord.abc.User) -> discord.Embed:
        total_members = guild.member_count or len(guild.members)
        humans = sum(1 for member in guild.members if not member.bot)
        bots = sum(1 for member in guild.members if member.bot)
        online = _count_online_members(guild)
        in_voice = _count_voice_members(guild)
        boosters = guild.premium_subscription_count or 0
        recent_joins = _count_recent_members(guild, days=7)
        created_ts = int(guild.created_at.timestamp())

        embed = discord.Embed(
            title=f"Member overview for {guild.name}",
            description="A quick look at how the server is made up right now.",
            color=discord.Color.blurple(),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.add_field(name="Total members", value=f"`{total_members:,}`", inline=True)
        embed.add_field(name="Humans", value=f"`{humans:,}`", inline=True)
        embed.add_field(name="Bots", value=f"`{bots:,}`", inline=True)

        embed.add_field(name="Online now", value=f"`{online:,}`", inline=True)
        embed.add_field(name="In voice", value=f"`{in_voice:,}`", inline=True)
        embed.add_field(name="Joined this week", value=f"`{recent_joins:,}`", inline=True)

        embed.add_field(name="Boosters", value=f"`{boosters:,}`", inline=True)
        embed.add_field(name="Verification", value=f"`{str(guild.verification_level).replace('_', ' ').title()}`", inline=True)
        embed.add_field(name="Created", value=f"<t:{created_ts}:R>", inline=True)

        embed.set_footer(text=f"Requested by {requester.display_name}", icon_url=requester.display_avatar.url)
        return embed

    def _build_roles_embed(self, guild: discord.Guild, requester: discord.abc.User) -> discord.Embed:
        embed = discord.Embed(
            title=f"Role distribution for {guild.name}",
            description="See which roles are shaping most of the member base.",
            color=discord.Color.blurple(),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        total_members = max(guild.member_count or len(guild.members), 1)
        ranked_roles = _top_roles(guild)
        if ranked_roles:
            lines = []
            for role, member_count in ranked_roles:
                percent = (member_count / total_members) * 100
                lines.append(f"{role.mention} - `{member_count:,}` members ({percent:.0f}%)")
            embed.add_field(name="Most used roles", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Most used roles", value="No non-default roles have members yet.", inline=False)

        managed_roles = sum(1 for role in guild.roles if role.managed)
        mentionable_roles = sum(1 for role in guild.roles if role.mentionable and not role.is_default())
        hoisted_roles = sum(1 for role in guild.roles if role.hoist and not role.is_default())
        embed.add_field(name="Total roles", value=f"`{max(len(guild.roles) - 1, 0):,}`", inline=True)
        embed.add_field(name="Mentionable", value=f"`{mentionable_roles:,}`", inline=True)
        embed.add_field(name="Displayed separately", value=f"`{hoisted_roles:,}`", inline=True)
        embed.add_field(name="Managed roles", value=f"`{managed_roles:,}`", inline=True)
        embed.add_field(name="Role slots left", value=f"`{max(250 - len(guild.roles), 0):,}`", inline=True)
        embed.add_field(name="Default role", value=guild.default_role.mention, inline=True)
        embed.set_footer(text=f"Requested by {requester.display_name}", icon_url=requester.display_avatar.url)
        return embed

    def _build_activity_embed(self, guild: discord.Guild, requester: discord.abc.User) -> discord.Embed:
        embed = discord.Embed(
            title=f"Activity snapshot for {guild.name}",
            description="Useful live numbers for onboarding, activity, and staff awareness.",
            color=discord.Color.blurple(),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        joined_today = _count_recent_members(guild, days=1)
        joined_week = _count_recent_members(guild, days=7)
        joined_month = _count_recent_members(guild, days=30)
        online = _count_online_members(guild)
        in_voice = _count_voice_members(guild)
        pending_screening = sum(1 for member in guild.members if getattr(member, "pending", False))
        staff = sum(
            1
            for member in guild.members
            if member.guild_permissions.administrator or member.guild_permissions.manage_guild
        )

        embed.add_field(name="Joined today", value=f"`{joined_today:,}`", inline=True)
        embed.add_field(name="Joined in 7 days", value=f"`{joined_week:,}`", inline=True)
        embed.add_field(name="Joined in 30 days", value=f"`{joined_month:,}`", inline=True)
        embed.add_field(name="Online now", value=f"`{online:,}`", inline=True)
        embed.add_field(name="In voice", value=f"`{in_voice:,}`", inline=True)
        embed.add_field(name="Pending screening", value=f"`{pending_screening:,}`", inline=True)
        embed.add_field(name="Staff members", value=f"`{staff:,}`", inline=True)
        embed.add_field(name="Bots", value=f"`{sum(1 for member in guild.members if member.bot):,}`", inline=True)
        embed.add_field(name="Humans", value=f"`{sum(1 for member in guild.members if not member.bot):,}`", inline=True)
        embed.set_footer(text=f"Requested by {requester.display_name}", icon_url=requester.display_avatar.url)
        return embed

    @app_commands.command(name="membercount", description="See your server's member totals and breakdowns")
    @app_commands.describe(view="Choose which member view you want to see")
    @app_commands.choices(
        view=[
            app_commands.Choice(name="Overview", value="overview"),
            app_commands.Choice(name="Roles", value="roles"),
            app_commands.Choice(name="Activity", value="activity"),
        ]
    )
    async def membercount(
        self,
        interaction: discord.Interaction,
        view: app_commands.Choice[str] | None = None,
    ) -> None:
        guild = self._require_guild(interaction)
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        selected_view: Literal["overview", "roles", "activity"] = (
            view.value if view is not None else "overview"
        )

        if selected_view == "roles":
            embed = self._build_roles_embed(guild, interaction.user)
        elif selected_view == "activity":
            embed = self._build_activity_embed(guild, interaction.user)
        else:
            embed = self._build_overview_embed(guild, interaction.user)

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MemberCount(bot))
