from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands


PurgeMode = Literal["all", "bots", "humans", "links", "attachments", "embeds", "mentions"]


def _looks_like_link(content: str) -> bool:
    lowered = content.lower()
    return "http://" in lowered or "https://" in lowered or "discord.gg/" in lowered or "discord.com/invite/" in lowered


def _message_matches(
    message: discord.Message,
    *,
    mode: PurgeMode,
    target_user_id: int | None,
    contains: str | None,
    include_pinned: bool,
) -> bool:
    if message.pinned and not include_pinned:
        return False
    if target_user_id is not None and message.author.id != target_user_id:
        return False

    cleaned_contains = (contains or "").strip().lower()
    content = (message.content or "").lower()
    if cleaned_contains and cleaned_contains not in content:
        return False

    if mode == "bots":
        return message.author.bot
    if mode == "humans":
        return not message.author.bot
    if mode == "links":
        return _looks_like_link(message.content or "")
    if mode == "attachments":
        return bool(message.attachments)
    if mode == "embeds":
        return bool(message.embeds)
    if mode == "mentions":
        return bool(message.mentions or message.role_mentions or message.mention_everyone)
    return True


def _scan_limit(amount: int) -> int:
    return min(max(amount * 5, 50), 1000)


def _filter_summary_parts(
    *,
    mode: PurgeMode,
    target_user: discord.Member | None,
    contains: str | None,
    include_pinned: bool,
) -> list[str]:
    parts = [f"mode: {mode}"]
    if target_user is not None:
        parts.append(f"user: {target_user.display_name}")
    if contains:
        parts.append(f'match: "{contains}"')
    if include_pinned:
        parts.append("pinned included")
    return parts


class Purge(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def can_manage(self, member: discord.Member) -> bool:
        return member.guild_permissions.administrator or member.guild_permissions.manage_messages

    async def _log_purge(
        self,
        guild: discord.Guild,
        *,
        moderator: discord.abc.User,
        channel_name: str | None,
        summary: str,
        fields: Iterable[tuple[str, str, bool]] | None = None,
    ) -> None:
        audit_cog = self.bot.get_cog("AuditLogCog")
        if audit_cog is not None and hasattr(audit_cog, "emit_external_event"):
            await audit_cog.emit_external_event(
                guild.id,
                title="Purge Run",
                description=summary,
                status="event",
                color=discord.Color.orange(),
                user_name=str(moderator),
                channel_name=channel_name,
                fields=list(fields or []),
            )

    @app_commands.command(name="purge", description="Delete recent messages with more targeted filters")
    @app_commands.describe(
        amount="How many matching messages should be deleted",
        user="Optional: only delete messages from this member",
        mode="Choose which kinds of messages to target",
        contains="Optional: only delete messages containing this text",
        include_pinned="Override whether pinned messages can be included",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Everything", value="all"),
            app_commands.Choice(name="Bots only", value="bots"),
            app_commands.Choice(name="Humans only", value="humans"),
            app_commands.Choice(name="Links", value="links"),
            app_commands.Choice(name="Attachments", value="attachments"),
            app_commands.Choice(name="Embeds", value="embeds"),
            app_commands.Choice(name="Mentions", value="mentions"),
        ]
    )
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 2000],
        user: discord.Member | None = None,
        mode: app_commands.Choice[str] | None = None,
        contains: app_commands.Range[str, 1, 80] | None = None,
        include_pinned: bool | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("This command only works in a server.", ephemeral=True)
            return

        if not self.can_manage(interaction.user):
            await interaction.followup.send("You need Manage Messages or Administrator to use purge.", ephemeral=True)
            return

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send("Purge only works in text channels and threads.", ephemeral=True)
            return

        me = interaction.guild.me
        if me is None or not me.guild_permissions.manage_messages:
            await interaction.followup.send("I need Manage Messages before I can clean up messages here.", ephemeral=True)
            return

        configured_limit = self.bot.command_controls.get_purge_limit(interaction.guild.id)
        if amount > configured_limit:
            await interaction.followup.send(f"Choose a number between 1 and {configured_limit}.", ephemeral=True)
            return

        settings = self.bot.command_controls.get_purge_settings(interaction.guild.id)
        selected_mode: PurgeMode = (
            mode.value if mode is not None else settings.get("default_mode", "all")
        )
        include_pinned_effective = (
            settings.get("include_pinned_default", False) if include_pinned is None else include_pinned
        )
        max_age = datetime.now(UTC) - timedelta(days=14)
        matched = 0

        def check(message: discord.Message) -> bool:
            nonlocal matched
            if matched >= amount:
                return False
            if message.created_at < max_age:
                return False
            if _message_matches(
                message,
                mode=selected_mode,
                target_user_id=user.id if user else None,
                contains=contains,
                include_pinned=include_pinned_effective,
            ):
                matched += 1
                return True
            return False

        try:
            deleted = await channel.purge(limit=_scan_limit(amount), check=check)
        except discord.Forbidden:
            await interaction.followup.send("I need Manage Messages and channel access to do that.", ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.followup.send("I couldn't finish the purge right now. Please try again.", ephemeral=True)
            return

        if not deleted:
            await interaction.followup.send("I didn't find any recent messages that matched those filters.", ephemeral=True)
            return

        summary_parts = _filter_summary_parts(
            mode=selected_mode,
            target_user=user,
            contains=contains,
            include_pinned=include_pinned_effective,
        )
        summary = f"Deleted {len(deleted)} message(s) in {channel.mention}. Filters: {', '.join(summary_parts)}."
        await interaction.followup.send(summary, ephemeral=True)
        await self._log_purge(
            interaction.guild,
            moderator=interaction.user,
            channel_name=getattr(channel, "name", None),
            summary=summary,
            fields=[
                ("Deleted", str(len(deleted)), True),
                ("Filters", ", ".join(summary_parts), False),
            ],
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Purge(bot))
