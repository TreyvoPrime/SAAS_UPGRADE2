from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from core.giveaways import GiveawayStore, from_iso


MAX_GIVEAWAY_DAYS = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _format_remaining(ends_at: datetime) -> str:
    seconds = max(int((ends_at - _utcnow()).total_seconds()), 0)
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


def _role_mentions(guild: discord.Guild | None, role_ids: list[int]) -> str:
    if guild is None:
        return "Configured role"
    mentions = [
        guild.get_role(int(role_id)).mention
        for role_id in role_ids
        if guild.get_role(int(role_id)) is not None
    ]
    return ", ".join(mentions) if mentions else "None"


class GiveawayEnterView(discord.ui.View):
    def __init__(self, cog: GiveawayCog, giveaway_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="Enter giveaway", style=discord.ButtonStyle.success, custom_id="servercore:giveaway:enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_entry(interaction, self.giveaway_id)


class GiveawayCog(commands.Cog):
    giveaway = app_commands.Group(
        name="giveaway",
        description="Create and manage giveaways in your server",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store: GiveawayStore = bot.giveaway_store
        self._end_loop_task: asyncio.Task | None = None

    def _premium_enabled(self, guild_id: int) -> bool:
        controls = getattr(self.bot, "command_controls", None)
        return bool(controls and hasattr(controls, "is_premium_enabled") and controls.is_premium_enabled(guild_id))

    async def cog_load(self) -> None:
        for record in self.store.all_active():
            message_id = int(record.get("message_id") or 0)
            if message_id:
                self.bot.add_view(GiveawayEnterView(self, int(record["id"])), message_id=message_id)
        if self._end_loop_task is None:
            self._end_loop_task = asyncio.create_task(self._end_loop(), name="giveaway-end-loop")

    def cog_unload(self) -> None:
        if self._end_loop_task:
            self._end_loop_task.cancel()
            self._end_loop_task = None

    async def _end_loop(self) -> None:
        while True:
            try:
                await self._end_due_giveaways()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(20)

    async def _require_giveaway_staff(self, interaction: discord.Interaction) -> discord.Member | None:
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
            await interaction.response.send_message("You need Manage Server, Manage Messages, or Administrator to manage giveaways.", ephemeral=True)
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
                color=discord.Color.gold(),
                user_name=user_name,
                channel_name=channel_name,
                fields=fields,
            )

    def _build_embed(self, record: dict, *, ended: bool = False) -> discord.Embed:
        ends_at = from_iso(record.get("ends_at"))
        entrant_count = len(record.get("entrants", []))
        title = record.get("prize", "Giveaway")
        description = record.get("description") or "Click the button below to enter."
        color = discord.Color.gold() if not ended else discord.Color.blurple()
        embed = discord.Embed(title=f"🎉 {title}", description=description, color=color)
        embed.add_field(name="Hosted by", value=record.get("host_name", "Unknown"), inline=True)
        embed.add_field(name="Winners", value=str(record.get("winner_count", 1)), inline=True)
        embed.add_field(name="Entries", value=str(entrant_count), inline=True)
        if record.get("required_role_ids"):
            embed.add_field(name="Required role", value=_role_mentions(self.bot.get_guild(int(record["guild_id"])), record.get("required_role_ids", [])), inline=False)
        if record.get("bonus_role_ids"):
            embed.add_field(
                name="Bonus entries",
                value=f"{record.get('bonus_entries', 0)} extra for {_role_mentions(self.bot.get_guild(int(record['guild_id'])), record.get('bonus_role_ids', []))}",
                inline=False,
            )
        if ends_at is not None and not ended:
            embed.add_field(name="Ends", value=f"<t:{int(ends_at.timestamp())}:F>\n({_format_remaining(ends_at)} left)", inline=False)
        if ended:
            winner_names = record.get("winner_names", [])
            embed.add_field(name="Result", value=", ".join(winner_names) if winner_names else "No valid entries", inline=False)
            if record.get("ended_at"):
                ended_at = from_iso(record.get("ended_at"))
                if ended_at:
                    embed.set_footer(text=f"Ended at {ended_at.strftime('%Y-%m-%d %H:%M UTC')}")
        else:
            embed.set_footer(text=f"Giveaway #{record['id']}")
        return embed

    async def _fetch_message(self, guild: discord.Guild, record: dict) -> discord.Message | None:
        channel = guild.get_channel(int(record.get("channel_id") or 0))
        if not isinstance(channel, discord.TextChannel):
            return None
        message_id = int(record.get("message_id") or 0)
        if not message_id:
            return None
        try:
            return await channel.fetch_message(message_id)
        except Exception:
            return None

    async def _end_due_giveaways(self) -> None:
        now = _utcnow()
        for record in self.store.all_active():
            ends_at = from_iso(record.get("ends_at"))
            if ends_at and ends_at <= now:
                await self._finish_giveaway(int(record["guild_id"]), int(record["id"]), forced_by=None)

    async def _finish_giveaway(self, guild_id: int, giveaway_id: int, forced_by: discord.abc.User | None) -> dict | None:
        record = self.store.get_giveaway(guild_id, giveaway_id)
        if record is None or record.get("status") != "active":
            return None
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None

        name_lookup: dict[int, str] = {}
        for entrant_id in record.get("entrants", []):
            member = guild.get_member(int(entrant_id))
            if member is not None:
                name_lookup[int(entrant_id)] = member.mention
        winner_ids, winner_names = self.store.choose_winners(record, name_lookup=name_lookup)
        record = self.store.end_giveaway(guild_id, giveaway_id, winner_ids=winner_ids, winner_names=winner_names)
        if record is None:
            return None

        message = await self._fetch_message(guild, record)
        if message is not None:
            try:
                await message.edit(embed=self._build_embed(record, ended=True), view=None)
            except Exception:
                pass
            try:
                if winner_names:
                    await message.reply(f"🎉 Giveaway ended. Congratulations {', '.join(winner_names)}!", mention_author=False)
                else:
                    await message.reply("Giveaway ended, but there were no valid entries.", mention_author=False)
            except Exception:
                pass

        await self._log_event(
            guild,
            title="Giveaway Ended",
            description=f"Giveaway #{giveaway_id} ended for **{record['prize']}**.",
            user_name=str(forced_by) if forced_by else "Giveaway timer",
            channel_name=getattr(message.channel, "name", None) if message else None,
            fields=[
                ("Winners", ", ".join(winner_names) if winner_names else "No valid entries", False),
                ("Entries", str(len(record.get("entrants", []))), True),
            ],
        )
        return record

    async def handle_entry(self, interaction: discord.Interaction, giveaway_id: int) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This giveaway entry only works inside a server.", ephemeral=True)
            return
        if interaction.user.bot:
            await interaction.response.send_message("Bots can't enter giveaways.", ephemeral=True)
            return
        record = self.store.get_giveaway(interaction.guild.id, giveaway_id)
        if record is None or record.get("status") != "active":
            await interaction.response.send_message("That giveaway is no longer active.", ephemeral=True)
            return
        ends_at = from_iso(record.get("ends_at"))
        if ends_at and ends_at <= _utcnow():
            await interaction.response.send_message("That giveaway is already ending.", ephemeral=True)
            return
        required_role_ids = [int(role_id) for role_id in record.get("required_role_ids", [])]
        if required_role_ids and not any(role.id in required_role_ids for role in interaction.user.roles):
            await interaction.response.send_message("You need the required giveaway role before you can enter this one.", ephemeral=True)
            return
        bonus_role_ids = [int(role_id) for role_id in record.get("bonus_role_ids", [])]
        has_bonus = bool(bonus_role_ids) and any(role.id in bonus_role_ids for role in interaction.user.roles)
        entry_count = 1 + (int(record.get("bonus_entries", 0)) if has_bonus else 0)
        record, added = self.store.add_entries(interaction.guild.id, giveaway_id, interaction.user.id, entry_count=entry_count)
        if not added:
            await interaction.response.send_message("You're already entered in this giveaway.", ephemeral=True)
            return
        message = await self._fetch_message(interaction.guild, record)
        if message is not None:
            try:
                await message.edit(embed=self._build_embed(record, ended=False), view=GiveawayEnterView(self, giveaway_id))
            except Exception:
                pass
        bonus_line = f" You received {entry_count} entries." if entry_count > 1 else ""
        await interaction.response.send_message(f"You're in. Good luck in giveaway #{giveaway_id}.{bonus_line}", ephemeral=True)

    @giveaway.command(name="create", description="Create a giveaway in this server")
    @app_commands.describe(
        prize="What the giveaway winner gets",
        winners="How many winners to pick",
        channel="Where the giveaway should be posted",
        description="Optional extra detail or instructions",
        days="Days until it ends",
        hours="Hours until it ends",
        minutes="Minutes until it ends",
        required_role="Optional role members must have before they can enter",
        bonus_role="Optional role that unlocks bonus entries",
        bonus_entries="How many extra entries the bonus role gets",
    )
    async def giveaway_create(
        self,
        interaction: discord.Interaction,
        prize: str,
        winners: app_commands.Range[int, 1, 20] = 1,
        channel: discord.TextChannel | None = None,
        description: str | None = None,
        days: app_commands.Range[int, 0, MAX_GIVEAWAY_DAYS] = 0,
        hours: app_commands.Range[int, 0, 23] = 0,
        minutes: app_commands.Range[int, 0, 59] = 0,
        required_role: discord.Role | None = None,
        bonus_role: discord.Role | None = None,
        bonus_entries: app_commands.Range[int, 0, 20] = 0,
    ):
        staff = await self._require_giveaway_staff(interaction)
        if staff is None or interaction.guild is None:
            return
        target_channel = channel or interaction.channel
        if not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message("Choose a standard text channel for the giveaway.", ephemeral=True)
            return
        me = interaction.guild.me
        if me is None or not me.guild_permissions.send_messages:
            await interaction.response.send_message("I need Send Messages before I can post a giveaway.", ephemeral=True)
            return
        total_minutes = days * 1440 + hours * 60 + minutes
        if total_minutes <= 0:
            await interaction.response.send_message("Set at least one time value so the giveaway knows when to end.", ephemeral=True)
            return
        wants_premium_rules = bool(required_role or bonus_role or bonus_entries)
        if wants_premium_rules and not self._premium_enabled(interaction.guild.id):
            await interaction.response.send_message(
                "Role requirements and bonus entries are part of ServerCore Premium right now.",
                ephemeral=True,
            )
            return
        ends_at = _utcnow() + timedelta(minutes=total_minutes)
        record = self.store.create_giveaway(
            interaction.guild.id,
            channel_id=target_channel.id,
            host_id=interaction.user.id,
            host_name=str(interaction.user),
            prize=prize,
            description=description,
            winner_count=int(winners),
            ends_at=ends_at,
            required_role_ids=[required_role.id] if required_role else [],
            bonus_role_ids=[bonus_role.id] if bonus_role else [],
            bonus_entries=int(bonus_entries),
        )
        view = GiveawayEnterView(self, int(record["id"]))
        embed = self._build_embed(record, ended=False)
        message = await target_channel.send(embed=embed, view=view)
        record = self.store.set_message_id(interaction.guild.id, int(record["id"]), message.id) or record
        self.bot.add_view(view, message_id=message.id)
        await interaction.response.send_message(
            f"Giveaway #{record['id']} is live in {target_channel.mention} for **{record['prize']}**.",
            ephemeral=True,
        )
        await self._log_event(
            interaction.guild,
            title="Giveaway Created",
            description=f"Giveaway #{record['id']} was created for **{record['prize']}**.",
            user_name=str(interaction.user),
            channel_name=target_channel.name,
            fields=[
                ("Ends", ends_at.strftime("%Y-%m-%d %H:%M UTC"), True),
                ("Winners", str(record["winner_count"]), True),
                ("Required Role", required_role.name if required_role else "None", True),
                ("Bonus Entries", f"{bonus_entries} for {bonus_role.name}" if bonus_role and bonus_entries else "None", False),
            ],
        )

    @giveaway.command(name="end", description="End a giveaway early")
    async def giveaway_end(self, interaction: discord.Interaction, giveaway_id: app_commands.Range[int, 1, 1000000]):
        staff = await self._require_giveaway_staff(interaction)
        if staff is None or interaction.guild is None:
            return
        record = self.store.get_giveaway(interaction.guild.id, int(giveaway_id))
        if record is None:
            await interaction.response.send_message("I couldn't find that giveaway in this server.", ephemeral=True)
            return
        if record.get("status") != "active":
            await interaction.response.send_message("That giveaway has already ended.", ephemeral=True)
            return
        await self._finish_giveaway(interaction.guild.id, int(giveaway_id), forced_by=interaction.user)
        await interaction.response.send_message(f"Giveaway #{giveaway_id} has been ended.", ephemeral=True)

    @giveaway.command(name="list", description="List active and recent giveaways")
    async def giveaway_list(self, interaction: discord.Interaction):
        staff = await self._require_giveaway_staff(interaction)
        if staff is None or interaction.guild is None:
            return
        active = self.store.list_giveaways(interaction.guild.id, status="active", limit=10)
        recent = self.store.list_giveaways(interaction.guild.id, status="ended", limit=5)
        embed = discord.Embed(title="Giveaways", color=discord.Color.gold())
        embed.add_field(
            name="Active",
            value="\n".join(
                f"#{item['id']} • {item['prize']} • {_format_remaining(from_iso(item['ends_at']) or _utcnow())} left • {len(item.get('entrants', []))} entries"
                for item in active
            ) or "No active giveaways right now.",
            inline=False,
        )
        embed.add_field(
            name="Recently ended",
            value="\n".join(
                f"#{item['id']} • {item['prize']} • winners: {', '.join(item.get('winner_names', [])) or 'none'}"
                for item in recent
            ) or "No ended giveaways yet.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @giveaway.command(name="reroll", description="Pick new winners for an ended giveaway")
    async def giveaway_reroll(self, interaction: discord.Interaction, giveaway_id: app_commands.Range[int, 1, 1000000]):
        staff = await self._require_giveaway_staff(interaction)
        if staff is None or interaction.guild is None:
            return
        record = self.store.get_giveaway(interaction.guild.id, int(giveaway_id))
        if record is None:
            await interaction.response.send_message("I couldn't find that giveaway in this server.", ephemeral=True)
            return
        if record.get("status") != "ended":
            await interaction.response.send_message("You can only reroll a giveaway after it has ended.", ephemeral=True)
            return
        name_lookup: dict[int, str] = {}
        for entrant_id in record.get("entrants", []):
            member = interaction.guild.get_member(int(entrant_id))
            name_lookup[int(entrant_id)] = member.mention if member else f"User {entrant_id}"
        rerolled = self.store.reroll_giveaway(interaction.guild.id, int(giveaway_id), winner_names=name_lookup)
        if rerolled is None:
            await interaction.response.send_message("That giveaway does not have enough valid entries to reroll.", ephemeral=True)
            return
        message = await self._fetch_message(interaction.guild, rerolled)
        if message is not None:
            try:
                await message.edit(embed=self._build_embed(rerolled, ended=True), view=None)
                await message.reply(
                    f"🔁 Giveaway rerolled. New winner(s): {', '.join(rerolled.get('winner_names', [])) or 'none'}",
                    mention_author=False,
                )
            except Exception:
                pass
        await interaction.response.send_message(
            f"Rerolled giveaway #{giveaway_id}. New winner(s): {', '.join(rerolled.get('winner_names', [])) or 'none'}.",
            ephemeral=True,
        )
        await self._log_event(
            interaction.guild,
            title="Giveaway Rerolled",
            description=f"Giveaway #{giveaway_id} was rerolled for **{rerolled['prize']}**.",
            user_name=str(interaction.user),
            channel_name=getattr(message.channel, "name", None) if message else None,
            fields=[("Winners", ", ".join(rerolled.get("winner_names", [])) or "none", False)],
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(GiveawayCog(bot))
