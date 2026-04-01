from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from core.autofeed import AutoFeedStore, from_iso


def _format_minutes(total_minutes: int) -> str:
    days, remainder = divmod(int(total_minutes), 1440)
    hours, minutes = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


class AutoFeedCog(commands.Cog):
    autofeed = app_commands.Group(
        name="autofeed",
        description="Create and manage repeating auto-feed messages",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store: AutoFeedStore = bot.autofeed_store
        self._loop_task: asyncio.Task | None = None

    def _premium_enabled(self, guild_id: int) -> bool:
        controls = getattr(self.bot, "command_controls", None)
        return bool(controls and hasattr(controls, "is_premium_enabled") and controls.is_premium_enabled(guild_id))

    async def _require_premium_autofeed(self, interaction: discord.Interaction, feature_name: str) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return False
        if self._premium_enabled(interaction.guild.id):
            return True
        await interaction.response.send_message(
            f"{feature_name} is part of ServerCore Premium right now.",
            ephemeral=True,
        )
        return False

    async def cog_load(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._autofeed_loop(), name="autofeed-loop")

    def cog_unload(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None

    async def _require_staff(self, interaction: discord.Interaction) -> discord.Member | None:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return None
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("I couldn't verify your server permissions.", ephemeral=True)
            return None
        if not (
            interaction.user.guild_permissions.manage_guild
            or interaction.user.guild_permissions.manage_messages
            or interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message("You need Manage Server, Manage Messages, or Administrator to manage autofeeds.", ephemeral=True)
            return None
        return interaction.user

    async def _log_event(
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
                color=discord.Color.teal(),
                user_name=user_name,
                channel_name=channel_name,
                fields=fields,
            )

    async def _autofeed_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self._post_due_feeds()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(30)

    async def _post_due_feeds(self) -> None:
        now = datetime.now(timezone.utc)
        for feed in self.store.all_enabled():
            next_post_at = from_iso(feed.get("next_post_at"))
            if next_post_at is None or next_post_at > now:
                continue
            guild = self.bot.get_guild(int(feed["guild_id"]))
            if guild is None:
                continue
            channel = guild.get_channel(int(feed["channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                await channel.send(feed["message"])
            except Exception:
                continue
            self.store.update_after_post(int(feed["guild_id"]), int(feed["id"]))

    @autofeed.command(name="create", description="Create a repeating auto-feed in a channel")
    @app_commands.describe(
        channel="Where the auto-feed should post",
        message="The message that should repeat",
        days="Days between posts",
        hours="Hours between posts",
        minutes="Minutes between posts",
    )
    async def autofeed_create(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: app_commands.Range[str, 1, 1800],
        days: app_commands.Range[int, 0, 30] = 0,
        hours: app_commands.Range[int, 0, 23] = 0,
        minutes: app_commands.Range[int, 0, 59] = 0,
    ):
        staff = await self._require_staff(interaction)
        if staff is None or interaction.guild is None:
            return
        total_minutes = days * 1440 + hours * 60 + minutes
        if total_minutes <= 0:
            await interaction.response.send_message("Set at least one time value so the autofeed knows how often to post.", ephemeral=True)
            return
        record = self.store.create_feed(
            interaction.guild.id,
            channel_id=channel.id,
            created_by_id=interaction.user.id,
            created_by_name=str(interaction.user),
            message=message,
            interval_minutes=total_minutes,
        )
        next_post_at = from_iso(record.get("next_post_at"))
        await interaction.response.send_message(
            f"Autofeed #{record['id']} is live in {channel.mention}. It will post every {_format_minutes(total_minutes)} and next send at <t:{int(next_post_at.timestamp())}:F>.",
            ephemeral=True,
        )
        await self._log_event(
            interaction.guild,
            title="Autofeed Created",
            description=f"Autofeed #{record['id']} will post in {channel.mention}.",
            user_name=str(interaction.user),
            channel_name=channel.name,
            fields=[
                ("Interval", _format_minutes(total_minutes), True),
                ("Message", message[:200], False),
            ],
        )

    @autofeed.command(name="list", description="View the active autofeeds in this server")
    async def autofeed_list(self, interaction: discord.Interaction):
        staff = await self._require_staff(interaction)
        if staff is None or interaction.guild is None:
            return
        items = self.store.list_feeds(interaction.guild.id)
        embed = discord.Embed(title="Autofeeds", color=discord.Color.teal())
        embed.description = "\n\n".join(
            f"**#{item['id']}** in <#{item['channel_id']}> every {_format_minutes(item['interval_minutes'])}\nNext post: <t:{int((from_iso(item['next_post_at']) or datetime.now(timezone.utc)).timestamp())}:F>"
            for item in items[:10]
        ) or "No autofeeds are running in this server yet."
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @autofeed.command(name="delete", description="Delete one of the server's autofeeds")
    async def autofeed_delete(self, interaction: discord.Interaction, autofeed_id: app_commands.Range[int, 1, 1000000]):
        staff = await self._require_staff(interaction)
        if staff is None or interaction.guild is None:
            return
        removed = self.store.delete_feed(interaction.guild.id, int(autofeed_id))
        if not removed:
            await interaction.response.send_message("I couldn't find that autofeed in this server.", ephemeral=True)
            return
        await interaction.response.send_message(f"Autofeed #{autofeed_id} has been deleted.", ephemeral=True)

    @autofeed.command(name="pause", description="Pause a running autofeed without deleting it")
    async def autofeed_pause(self, interaction: discord.Interaction, autofeed_id: app_commands.Range[int, 1, 1000000]):
        if not await self._require_premium_autofeed(interaction, "Autofeed pause and resume controls"):
            return
        staff = await self._require_staff(interaction)
        if staff is None or interaction.guild is None:
            return
        updated = self.store.set_enabled(interaction.guild.id, int(autofeed_id), False)
        if updated is None:
            await interaction.response.send_message("I couldn't find that autofeed in this server.", ephemeral=True)
            return
        await interaction.response.send_message(f"Autofeed #{autofeed_id} is now paused.", ephemeral=True)

    @autofeed.command(name="resume", description="Resume a paused autofeed")
    async def autofeed_resume(self, interaction: discord.Interaction, autofeed_id: app_commands.Range[int, 1, 1000000]):
        if not await self._require_premium_autofeed(interaction, "Autofeed pause and resume controls"):
            return
        staff = await self._require_staff(interaction)
        if staff is None or interaction.guild is None:
            return
        updated = self.store.set_enabled(interaction.guild.id, int(autofeed_id), True)
        if updated is None:
            await interaction.response.send_message("I couldn't find that autofeed in this server.", ephemeral=True)
            return
        next_post_at = from_iso(updated.get("next_post_at"))
        next_line = f" Next post: <t:{int(next_post_at.timestamp())}:F>." if next_post_at else ""
        await interaction.response.send_message(f"Autofeed #{autofeed_id} is live again.{next_line}", ephemeral=True)

    @autofeed.command(name="edit", description="Edit an autofeed's channel, message, or interval")
    @app_commands.describe(
        autofeed_id="Which autofeed you want to update",
        channel="Optional new channel",
        message="Optional new repeating message",
        days="Optional new day interval",
        hours="Optional new hour interval",
        minutes="Optional new minute interval",
    )
    async def autofeed_edit(
        self,
        interaction: discord.Interaction,
        autofeed_id: app_commands.Range[int, 1, 1000000],
        channel: discord.TextChannel | None = None,
        message: app_commands.Range[str, 1, 1800] | None = None,
        days: app_commands.Range[int, 0, 30] | None = None,
        hours: app_commands.Range[int, 0, 23] | None = None,
        minutes: app_commands.Range[int, 0, 59] | None = None,
    ):
        if not await self._require_premium_autofeed(interaction, "Autofeed editing"):
            return
        staff = await self._require_staff(interaction)
        if staff is None or interaction.guild is None:
            return
        current = self.store.get_feed(interaction.guild.id, int(autofeed_id))
        if current is None:
            await interaction.response.send_message("I couldn't find that autofeed in this server.", ephemeral=True)
            return
        interval_minutes = None
        if days is not None or hours is not None or minutes is not None:
            interval_minutes = (days or 0) * 1440 + (hours or 0) * 60 + (minutes or 0)
            if interval_minutes <= 0:
                await interaction.response.send_message("If you change the interval, make it at least 1 minute total.", ephemeral=True)
                return
        updated = self.store.update_feed(
            interaction.guild.id,
            int(autofeed_id),
            channel_id=channel.id if channel else ...,
            message=message if message is not None else ...,
            interval_minutes=interval_minutes if interval_minutes is not None else ...,
        )
        if updated is None:
            await interaction.response.send_message("I couldn't update that autofeed right now.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Updated autofeed #{autofeed_id}. It now posts in <#{updated['channel_id']}> every {_format_minutes(updated['interval_minutes'])}.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoFeedCog(bot))
