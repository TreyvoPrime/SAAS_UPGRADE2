from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands


class RoleAlertConfirmView(discord.ui.View):
    def __init__(
        self,
        cog: "Alert",
        *,
        author_id: int,
        guild_id: int,
        role_id: int,
        message: str,
        vc_link: str | None,
        skip_in_voice: bool,
        only_offline: bool,
        include_bots: bool,
        cooldown_seconds: int,
    ) -> None:
        super().__init__(timeout=90)
        self.cog = cog
        self.author_id = author_id
        self.guild_id = guild_id
        self.role_id = role_id
        self.message = message
        self.vc_link = vc_link
        self.skip_in_voice = skip_in_voice
        self.only_offline = only_offline
        self.include_bots = include_bots
        self.cooldown_seconds = cooldown_seconds

    async def _reject_wrong_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the person who started this role alert can confirm it.", ephemeral=True)
            return True
        return False

    @discord.ui.button(label="Send now", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self._reject_wrong_user(interaction):
            return

        guild = self.cog.bot.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message("I couldn't find that server anymore.", ephemeral=True)
            return
        role = guild.get_role(self.role_id)
        if role is None:
            await interaction.response.send_message("I couldn't find that role anymore.", ephemeral=True)
            return

        allowed, cooldown_message = self.cog._consume_cooldown(guild.id, cooldown_seconds=self.cooldown_seconds)
        if not allowed:
            await interaction.response.send_message(cooldown_message or "This server is on cooldown right now.", ephemeral=True)
            return

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Sending role alert",
                description="The message is being delivered now. You'll get a result summary in a moment.",
                color=discord.Color.orange(),
            ),
            view=None,
        )

        sent, failed, skipped = await self.cog._send_role_alert(
            guild,
            role,
            message=self.message,
            vc_link=self.vc_link,
            skip_in_voice=self.skip_in_voice,
            only_offline=self.only_offline,
            include_bots=self.include_bots,
        )
        await self.cog._log_alert(
            guild,
            moderator=interaction.user,
            role=role,
            sent=sent,
            failed=failed,
            skipped=skipped,
        )

        result_lines = [
            f"Sent: `{sent}`",
            f"Failed: `{failed}`",
            f"Skipped voice: `{skipped['voice']}`",
            f"Skipped online: `{skipped['online']}`",
            f"Skipped bots: `{skipped['bots']}`",
        ]
        await interaction.followup.send(
            "Role alert finished.\n" + "\n".join(result_lines),
            ephemeral=True,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self._reject_wrong_user(interaction):
            return
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Role alert canceled",
                description="Nothing was sent.",
                color=discord.Color.dark_grey(),
            ),
            view=None,
        )


class Alert(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cooldowns: dict[int, datetime] = {}

    def is_admin(self, member: discord.Member) -> bool:
        return member.guild_permissions.administrator or member.guild_permissions.manage_guild

    def _get_settings(self, guild_id: int) -> dict:
        controls = getattr(self.bot, "command_controls", None)
        if controls is None or not hasattr(controls, "get_alert_settings"):
            return {
                "confirmation_enabled": True,
                "skip_in_voice_default": True,
                "only_offline_default": False,
                "include_bots_default": False,
                "cooldown_seconds": 120,
            }
        return controls.get_alert_settings(guild_id)

    def _consume_cooldown(self, guild_id: int, *, cooldown_seconds: int) -> tuple[bool, str | None]:
        now = datetime.now(UTC)
        current = self.cooldowns.get(guild_id)
        if current and now < current:
            remaining = int((current - now).total_seconds())
            return False, f"This server is on cooldown for another `{remaining}` seconds."
        self.cooldowns[guild_id] = now + timedelta(seconds=max(int(cooldown_seconds), 1))
        return True, None

    def _eligible_members(
        self,
        role: discord.Role,
        *,
        skip_in_voice: bool,
        only_offline: bool,
        include_bots: bool,
    ) -> tuple[list[discord.Member], dict[str, int]]:
        recipients: list[discord.Member] = []
        skipped = {"voice": 0, "online": 0, "bots": 0}

        for member in role.members:
            if member.bot and not include_bots:
                skipped["bots"] += 1
                continue
            if skip_in_voice and member.voice and member.voice.channel:
                skipped["voice"] += 1
                continue
            if only_offline and member.status != discord.Status.offline:
                skipped["online"] += 1
                continue
            recipients.append(member)

        return recipients, skipped

    async def _log_alert(
        self,
        guild: discord.Guild,
        *,
        moderator: discord.abc.User,
        role: discord.Role,
        sent: int,
        failed: int,
        skipped: dict[str, int],
    ) -> None:
        audit_cog = self.bot.get_cog("AuditLogCog")
        if audit_cog is not None and hasattr(audit_cog, "emit_external_event"):
            await audit_cog.emit_external_event(
                guild.id,
                title="Role Alert Sent",
                description=f"A role alert was sent to {role.mention}.",
                status="event",
                color=discord.Color.gold(),
                user_name=str(moderator),
                fields=[
                    ("Role", role.mention, True),
                    ("Sent", str(sent), True),
                    ("Failed", str(failed), True),
                    ("Skipped", f"voice={skipped['voice']}, online={skipped['online']}, bots={skipped['bots']}", False),
                ],
            )

    async def _send_role_alert(
        self,
        guild: discord.Guild,
        role: discord.Role,
        *,
        message: str,
        vc_link: str | None,
        skip_in_voice: bool,
        only_offline: bool,
        include_bots: bool,
    ) -> tuple[int, int, dict[str, int]]:
        recipients, skipped = self._eligible_members(
            role,
            skip_in_voice=skip_in_voice,
            only_offline=only_offline,
            include_bots=include_bots,
        )

        sent = 0
        failed = 0
        for member in recipients:
            try:
                content = f"Message from {guild.name}\n\n{message}"
                if vc_link:
                    content += f"\n\nJoin here: {vc_link}"
                await member.send(content)
                sent += 1
                await asyncio.sleep(0.3)
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
        return sent, failed, skipped

    @app_commands.command(name="dmrole", description="Send a staff-approved DM to members in a role")
    @app_commands.describe(
        role="Role to message",
        message="Message to send",
        vc_link="Optional voice channel or event link",
        skip_in_voice="Override whether members already in voice should be skipped",
        only_offline="Override whether only offline members should be included",
        include_bots="Override whether bots with this role should be included",
        confirm="Override whether this alert should ask for one last confirmation",
    )
    async def dmrole(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        message: app_commands.Range[str, 1, 1500],
        vc_link: app_commands.Range[str, 1, 300] | None = None,
        skip_in_voice: bool | None = None,
        only_offline: bool | None = None,
        include_bots: bool | None = None,
        confirm: bool | None = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        if not self.is_admin(interaction.user):
            await interaction.response.send_message("You need Manage Server or Administrator to use this.", ephemeral=True)
            return

        settings = self._get_settings(interaction.guild.id)
        effective_skip_in_voice = settings["skip_in_voice_default"] if skip_in_voice is None else skip_in_voice
        effective_only_offline = settings["only_offline_default"] if only_offline is None else only_offline
        effective_include_bots = settings["include_bots_default"] if include_bots is None else include_bots
        effective_confirm = settings["confirmation_enabled"] if confirm is None else confirm
        cooldown_seconds = int(settings["cooldown_seconds"])

        recipients, skipped = self._eligible_members(
            role,
            skip_in_voice=effective_skip_in_voice,
            only_offline=effective_only_offline,
            include_bots=effective_include_bots,
        )
        if not recipients:
            await interaction.response.send_message(
                "No eligible members match that role and filter set right now.",
                ephemeral=True,
            )
            return

        if not effective_confirm:
            allowed, cooldown_message = self._consume_cooldown(interaction.guild.id, cooldown_seconds=cooldown_seconds)
            if not allowed:
                await interaction.response.send_message(cooldown_message or "This server is on cooldown right now.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)
            sent, failed, skipped = await self._send_role_alert(
                interaction.guild,
                role,
                message=message,
                vc_link=vc_link,
                skip_in_voice=effective_skip_in_voice,
                only_offline=effective_only_offline,
                include_bots=effective_include_bots,
            )
            await self._log_alert(
                interaction.guild,
                moderator=interaction.user,
                role=role,
                sent=sent,
                failed=failed,
                skipped=skipped,
            )
            await interaction.followup.send(
                (
                    f"Role alert sent.\n"
                    f"Sent: `{sent}`\n"
                    f"Failed: `{failed}`\n"
                    f"Skipped voice: `{skipped['voice']}`\n"
                    f"Skipped online: `{skipped['online']}`\n"
                    f"Skipped bots: `{skipped['bots']}`"
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Review role alert",
            description="Check the target size before you send this. Nothing will go out until you confirm.",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Role", value=role.mention, inline=True)
        embed.add_field(name="Will receive it", value=f"`{len(recipients)}`", inline=True)
        embed.add_field(
            name="Skipped",
            value=f"voice `{skipped['voice']}` | online `{skipped['online']}` | bots `{skipped['bots']}`",
            inline=True,
        )
        embed.add_field(name="Cooldown", value=f"`{cooldown_seconds}` seconds", inline=True)
        embed.add_field(name="Message preview", value=message[:1000], inline=False)
        if vc_link:
            embed.add_field(name="Link included", value=vc_link, inline=False)

        view = RoleAlertConfirmView(
            self,
            author_id=interaction.user.id,
            guild_id=interaction.guild.id,
            role_id=role.id,
            message=message,
            vc_link=vc_link,
            skip_in_voice=effective_skip_in_voice,
            only_offline=effective_only_offline,
            include_bots=effective_include_bots,
            cooldown_seconds=cooldown_seconds,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Alert(bot))
