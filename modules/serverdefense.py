from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands


MAX_DURATION_MINUTES = 10080
MAX_TIMEOUT_MINUTES = 40320


class ModerationConfirmView(discord.ui.View):
    def __init__(self, actor_id: int, action: Callable[[], Awaitable[str]]):
        super().__init__(timeout=60)
        self.actor_id = actor_id
        self.action = action

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("This confirmation is not for you.", ephemeral=True)
            return

        self.disable_all_items()
        try:
            await interaction.response.edit_message(
                content="Working on that now...",
                view=self,
            )
        except Exception:
            await interaction.response.send_message("Working on that now...", ephemeral=True)
        try:
            result = await self.action()
        except discord.Forbidden:
            result = "Discord blocked that moderation action."
        except discord.HTTPException:
            result = "I couldn't complete that moderation action right now."
        except Exception:
            result = "Something went wrong while completing that action."
        try:
            await interaction.message.edit(content="Action completed.", view=None)
        except Exception:
            pass
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("This confirmation is not for you.", ephemeral=True)
            return
        self.disable_all_items()
        try:
            await interaction.response.edit_message(content="Action canceled.", view=None)
        except Exception:
            await interaction.response.send_message("Action canceled.", ephemeral=True)


class ServerDefense(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    def _moderation_embed(title: str, description: str, *, color: discord.Color) -> discord.Embed:
        embed = discord.Embed(title=title, description=description, color=color)
        return embed

    linkblock = app_commands.Group(
        name="linkblock",
        description="Manage link blocking for this server",
    )
    inviteblock = app_commands.Group(
        name="inviteblock",
        description="Manage invite-link blocking for this server",
    )
    antispam = app_commands.Group(
        name="antispam",
        description="Manage anti-spam protection for this server",
    )
    antijoin = app_commands.Group(
        name="antijoin",
        description="Prevent new users from staying in the server",
    )
    mentionguard = app_commands.Group(
        name="mentionguard",
        description="Block mention spam bursts in this server",
    )
    lockdown = app_commands.Group(
        name="lockdown",
        description="Lock or unlock the server's text channels",
    )
    serverguard = app_commands.Group(
        name="serverguard",
        description="Arm or release the full ServerGuard defensive stack",
    )

    async def _require_moderator(self, interaction: discord.Interaction) -> discord.Member | None:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return None

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("I couldn't verify your server permissions.", ephemeral=True)
            return None

        if not (
            interaction.user.guild_permissions.administrator
            or interaction.user.guild_permissions.manage_guild
            or interaction.user.guild_permissions.manage_messages
            or interaction.user.guild_permissions.moderate_members
            or interaction.user.guild_permissions.kick_members
            or interaction.user.guild_permissions.ban_members
        ):
            await interaction.response.send_message(
                "You need moderation permissions to use this command.",
                ephemeral=True,
            )
            return None

        return interaction.user

    async def _require_admin(self, interaction: discord.Interaction) -> discord.Member | None:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return None

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("I couldn't verify your server permissions.", ephemeral=True)
            return None

        if not (
            interaction.user.guild_permissions.manage_guild
            or interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server or Administrator to use ServerDefense commands.",
                ephemeral=True,
            )
            return None

        return interaction.user

    async def _ensure_bot_permissions(self, interaction: discord.Interaction, **permissions: bool) -> bool:
        guild = interaction.guild
        if guild is None or guild.me is None:
            await interaction.response.send_message("I couldn't verify my permissions right now.", ephemeral=True)
            return False

        missing = [
            label.replace("_", " ").title()
            for label, required in permissions.items()
            if required and not getattr(guild.me.guild_permissions, label, False)
        ]
        if missing:
            await interaction.response.send_message(
                f"I need these permissions first: {', '.join(missing)}.",
                ephemeral=True,
            )
            return False
        return True

    def _state_line(self, state: dict) -> str:
        if not state.get("enabled"):
            return "Disabled"
        ends_at = state.get("ends_at")
        return f"Enabled until `{ends_at}`" if ends_at else "Enabled until manually disabled"

    async def _send_status(self, interaction: discord.Interaction, feature: str, title: str, extra_lines: list[str] | None = None):
        assert interaction.guild is not None
        state = self.bot.server_defense.get_dashboard_state(interaction.guild.id)[feature]
        embed = discord.Embed(title=title, color=discord.Color.blurple())
        embed.add_field(name="State", value=self._state_line(state), inline=False)
        for line in extra_lines or []:
            name, value = line.split(":", 1)
            embed.add_field(name=name, value=value.strip(), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _enable_feature(
        self,
        interaction: discord.Interaction,
        feature: str,
        label: str,
        *,
        duration_minutes: int | None = None,
        permission_requirements: dict[str, bool] | None = None,
    ) -> None:
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return

        if permission_requirements and not await self._ensure_bot_permissions(interaction, **permission_requirements):
            return

        state = await self.bot.server_defense.enable_feature(
            interaction.guild.id,
            feature,
            duration_minutes=duration_minutes,
            actor=interaction.user,
        )
        await interaction.response.send_message(
            f"{label} enabled.{self._duration_message(state)}",
            ephemeral=True,
        )

    async def _disable_feature(self, interaction: discord.Interaction, feature: str, label: str) -> None:
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return

        await self.bot.server_defense.disable_feature(
            interaction.guild.id,
            feature,
            actor=interaction.user,
        )
        await interaction.response.send_message(f"{label} disabled.", ephemeral=True)

    def _duration_message(self, state: dict) -> str:
        ends_at = state.get("ends_at")
        if ends_at:
            return f" It will end at `{ends_at}`."
        return ""

    def _all_features_summary(self, guild_id: int) -> str:
        state = self.bot.server_defense.get_dashboard_state(guild_id)
        armed = [feature.replace("mentionguard", "mention guard") for feature in state if feature in {"linkblock", "inviteblock", "antispam", "antijoin", "mentionguard", "lockdown"} and state[feature].get("enabled")]
        return ", ".join(armed) if armed else "none"

    def _moderation_settings(self, guild_id: int) -> dict:
        return self.bot.command_controls.get_moderation_settings(guild_id)

    async def _confirm_or_run(
        self,
        interaction: discord.Interaction,
        prompt: str,
        action: Callable[[], Awaitable[str]],
    ) -> None:
        assert interaction.guild is not None
        settings = self._moderation_settings(interaction.guild.id)
        if settings.get("confirmation_enabled", True):
            await interaction.response.send_message(
                prompt,
                ephemeral=True,
                view=ModerationConfirmView(interaction.user.id, action),
            )
            return

        try:
            result = await action()
        except discord.Forbidden:
            result = "Discord blocked that moderation action."
        except discord.HTTPException:
            result = "I couldn't complete that moderation action right now."
        await interaction.response.send_message(result, ephemeral=True)

    async def _validate_moderation_target(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        *,
        action_name: str,
        require_bot_permission: str | None = None,
    ) -> bool:
        assert interaction.guild is not None
        assert isinstance(interaction.user, discord.Member)

        if member.id == interaction.user.id:
            await interaction.response.send_message(f"You can't {action_name} yourself.", ephemeral=True)
            return False
        if member.id == self.bot.user.id:
            await interaction.response.send_message(f"You can't {action_name} the bot.", ephemeral=True)
            return False
        if member.id == interaction.guild.owner_id:
            await interaction.response.send_message(f"You can't {action_name} the server owner.", ephemeral=True)
            return False
        if interaction.guild.owner_id != interaction.user.id and member.top_role >= interaction.user.top_role:
            await interaction.response.send_message(
                f"You can only {action_name} members below your top role.",
                ephemeral=True,
            )
            return False

        me = interaction.guild.me
        if me is None:
            await interaction.response.send_message("I couldn't verify my moderation permissions right now.", ephemeral=True)
            return False
        if require_bot_permission and not getattr(me.guild_permissions, require_bot_permission, False):
            await interaction.response.send_message(
                f"I need {require_bot_permission.replace('_', ' ').title()} to do that.",
                ephemeral=True,
            )
            return False
        if member.top_role >= me.top_role:
            await interaction.response.send_message(
                f"I can only {action_name} members below my top role.",
                ephemeral=True,
            )
            return False

        return True

    async def _send_warning_dm(
        self,
        member: discord.Member,
        guild_name: str,
        reason: str,
    ) -> bool:
        embed = self._moderation_embed(
            "You received a warning",
            f"You were warned in **{guild_name}**.",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text="Only you can see this message.")
        try:
            await member.send(embed=embed)
            return True
        except Exception:
            return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self.bot.server_defense.process_message(message)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        removed = await self.bot.server_defense.handle_member_join(member)
        if removed:
            channel = member.guild.system_channel
            if channel is not None:
                try:
                    await channel.send(
                        f"Anti-join removed `{member}` while ServerDefense anti-join was active.",
                        delete_after=8,
                    )
                except Exception:
                    pass

    @linkblock.command(name="enable", description="Block external links in this server")
    async def linkblock_enable(
        self,
        interaction: discord.Interaction,
        duration_minutes: app_commands.Range[int, 1, MAX_DURATION_MINUTES] | None = None,
    ):
        await self._enable_feature(
            interaction,
            "linkblock",
            "Link Block",
            duration_minutes=duration_minutes,
            permission_requirements={"manage_messages": True},
        )

    @linkblock.command(name="disable", description="Turn off link blocking")
    async def linkblock_disable(self, interaction: discord.Interaction):
        await self._disable_feature(interaction, "linkblock", "Link Block")

    @linkblock.command(name="status", description="View the current link block status")
    async def linkblock_status(self, interaction: discord.Interaction):
        member = await self._require_admin(interaction)
        if member is None:
            return
        await self._send_status(
            interaction,
            "linkblock",
            "Link Block",
            ["Behavior: Deletes messages with external URLs while active."],
        )

    @inviteblock.command(name="enable", description="Block Discord invite links in this server")
    async def inviteblock_enable(
        self,
        interaction: discord.Interaction,
        duration_minutes: app_commands.Range[int, 1, MAX_DURATION_MINUTES] | None = None,
    ):
        await self._enable_feature(
            interaction,
            "inviteblock",
            "Invite Block",
            duration_minutes=duration_minutes,
            permission_requirements={"manage_messages": True},
        )

    @inviteblock.command(name="disable", description="Turn off invite blocking")
    async def inviteblock_disable(self, interaction: discord.Interaction):
        await self._disable_feature(interaction, "inviteblock", "Invite Block")

    @inviteblock.command(name="status", description="View the current invite block status")
    async def inviteblock_status(self, interaction: discord.Interaction):
        member = await self._require_admin(interaction)
        if member is None:
            return
        await self._send_status(
            interaction,
            "inviteblock",
            "Invite Block",
            ["Behavior: Deletes Discord invite links while active."],
        )

    @antispam.command(name="enable", description="Shut down rapid message bursts at 5 messages in 6 seconds")
    async def antispam_enable(
        self,
        interaction: discord.Interaction,
        duration_minutes: app_commands.Range[int, 1, MAX_DURATION_MINUTES] | None = None,
    ):
        await self._enable_feature(
            interaction,
            "antispam",
            "Anti-spam",
            duration_minutes=duration_minutes,
            permission_requirements={"manage_messages": True},
        )

    @antispam.command(name="disable", description="Turn off anti-spam")
    async def antispam_disable(self, interaction: discord.Interaction):
        await self._disable_feature(interaction, "antispam", "Anti-spam")

    @antispam.command(name="status", description="View the current anti-spam status")
    async def antispam_status(self, interaction: discord.Interaction):
        member = await self._require_admin(interaction)
        if member is None:
            return
        await self._send_status(
            interaction,
            "antispam",
            "Anti-spam",
            [
                "Rate limit: 5 messages in 6 seconds.",
                "Action: Clears the burst and attempts a short timeout.",
            ],
        )

    @antijoin.command(name="enable", description="Kick new joins until the protection is disabled")
    async def antijoin_enable(
        self,
        interaction: discord.Interaction,
        duration_minutes: app_commands.Range[int, 1, MAX_DURATION_MINUTES] | None = None,
    ):
        await self._enable_feature(
            interaction,
            "antijoin",
            "Anti-join",
            duration_minutes=duration_minutes,
            permission_requirements={"kick_members": True},
        )

    @antijoin.command(name="disable", description="Turn off anti-join")
    async def antijoin_disable(self, interaction: discord.Interaction):
        await self._disable_feature(interaction, "antijoin", "Anti-join")

    @antijoin.command(name="status", description="View the current anti-join status")
    async def antijoin_status(self, interaction: discord.Interaction):
        member = await self._require_admin(interaction)
        if member is None:
            return
        await self._send_status(
            interaction,
            "antijoin",
            "Anti-join",
            ["Action: Kicks new members while protection is active."],
        )

    @mentionguard.command(name="enable", description="Block rapid mention bursts across messages")
    async def mentionguard_enable(
        self,
        interaction: discord.Interaction,
        duration_minutes: app_commands.Range[int, 1, MAX_DURATION_MINUTES] | None = None,
    ):
        await self._enable_feature(
            interaction,
            "mentionguard",
            "Mention Guard",
            duration_minutes=duration_minutes,
            permission_requirements={"manage_messages": True},
        )

    @mentionguard.command(name="disable", description="Turn off mention guard")
    async def mentionguard_disable(self, interaction: discord.Interaction):
        await self._disable_feature(interaction, "mentionguard", "Mention Guard")

    @mentionguard.command(name="status", description="View the current mention guard status")
    async def mentionguard_status(self, interaction: discord.Interaction):
        member = await self._require_admin(interaction)
        if member is None:
            return
        await self._send_status(
            interaction,
            "mentionguard",
            "Mention Guard",
            [
                "Mention limit: 5 mentions in 10 seconds.",
                "Action: Clears the burst and attempts a short timeout.",
            ],
        )

    @lockdown.command(name="enable", description="Lock the server's text channels")
    async def lockdown_enable(
        self,
        interaction: discord.Interaction,
        duration_minutes: app_commands.Range[int, 1, MAX_DURATION_MINUTES] | None = None,
    ):
        await self._enable_feature(
            interaction,
            "lockdown",
            "Server lockdown",
            duration_minutes=duration_minutes,
            permission_requirements={"manage_channels": True},
        )

    @lockdown.command(name="disable", description="Lift the active server lockdown")
    async def lockdown_disable(self, interaction: discord.Interaction):
        if not await self._ensure_bot_permissions(interaction, manage_channels=True):
            return
        await self._disable_feature(interaction, "lockdown", "Server lockdown")

    @lockdown.command(name="status", description="View the current lockdown status")
    async def lockdown_status(self, interaction: discord.Interaction):
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return

        state = self.bot.server_defense.get_dashboard_state(interaction.guild.id)["lockdown"]
        allowed_roles = [
            interaction.guild.get_role(role_id).mention
            for role_id in state.get("allowed_role_ids", [])
            if interaction.guild.get_role(role_id) is not None
        ]
        await self._send_status(
            interaction,
            "lockdown",
            "Server Lockdown",
            [
                "Allowed roles:"
                + (" " + ", ".join(allowed_roles) if allowed_roles else " No extra talk roles are configured."),
            ],
        )

    @lockdown.command(name="role", description="Add or remove a role that can still speak during lockdown")
    @app_commands.choices(mode=[
        app_commands.Choice(name="add", value="add"),
        app_commands.Choice(name="remove", value="remove"),
        app_commands.Choice(name="clear all", value="clear"),
    ])
    async def lockdown_role(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
        role: discord.Role | None = None,
    ):
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return

        state = self.bot.server_defense.get_dashboard_state(interaction.guild.id)["lockdown"]
        allowed_role_ids = set(state.get("allowed_role_ids", []))

        if mode.value == "clear":
            allowed_role_ids.clear()
        else:
            if role is None:
                await interaction.response.send_message("Pick a role to add or remove.", ephemeral=True)
                return
            if mode.value == "add":
                allowed_role_ids.add(role.id)
            elif mode.value == "remove":
                allowed_role_ids.discard(role.id)

        updated = await self.bot.server_defense.set_lockdown_roles(interaction.guild.id, sorted(allowed_role_ids))
        role_mentions = [
            interaction.guild.get_role(role_id).mention
            for role_id in updated.get("allowed_role_ids", [])
            if interaction.guild.get_role(role_id) is not None
        ]
        await interaction.response.send_message(
            "Updated lockdown talk roles.\n"
            f"Allowed roles: {', '.join(role_mentions) if role_mentions else 'No extra talk roles configured.'}",
            ephemeral=True,
        )

    @app_commands.command(name="warn", description="Warn a member and save the warning")
    @app_commands.describe(member="Member to warn", reason="Why they are being warned")
    async def warn(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ):
        moderator = await self._require_moderator(interaction)
        if moderator is None or interaction.guild is None:
            return

        if not await self._validate_moderation_target(interaction, member, action_name="warn"):
            return

        warning_reason = (reason or "No reason provided.").strip()

        async def action() -> str:
            self.bot.warning_store.add_warning(
                interaction.guild.id,
                member.id,
                moderator_id=interaction.user.id,
                reason=warning_reason,
            )
            warning_count = self.bot.warning_store.warning_count(interaction.guild.id, member.id)
            dm_sent = await self._send_warning_dm(member, interaction.guild.name, warning_reason)
            status_line = "They received a private copy of the warning." if dm_sent else "I couldn't deliver the private warning message, but the warning was still saved."
            return f"{member.mention} has been warned.\nReason: {warning_reason}\nTotal warnings: {warning_count}\n{status_line}"

        await self._confirm_or_run(
            interaction,
            f"Send a warning to {member.mention}?\nReason: {warning_reason}",
            action,
        )

    @app_commands.command(name="timeout", description="Timeout a member for a custom amount of time")
    @app_commands.describe(
        member="Member to timeout",
        duration_minutes="Minutes to timeout them for. Leave blank to use the ServerGuard default.",
        reason="Why they are being timed out",
    )
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration_minutes: app_commands.Range[int, 1, MAX_TIMEOUT_MINUTES] | None = None,
        reason: str | None = None,
    ):
        moderator = await self._require_moderator(interaction)
        if moderator is None or interaction.guild is None:
            return

        if not await self._validate_moderation_target(
            interaction,
            member,
            action_name="timeout",
            require_bot_permission="moderate_members",
        ):
            return

        settings = self._moderation_settings(interaction.guild.id)
        timeout_minutes = int(duration_minutes or settings["default_timeout_minutes"])
        timeout_reason = (reason or "No reason provided.").strip()

        async def action() -> str:
            await member.timeout(
                discord.utils.utcnow() + timedelta(minutes=timeout_minutes),
                reason=f"{interaction.user}: {timeout_reason}",
            )
            return f"{member.mention} has been timed out for {timeout_minutes} minutes."

        await self._confirm_or_run(
            interaction,
            f"Timeout {member.mention} for {timeout_minutes} minutes?\nReason: {timeout_reason}",
            action,
        )

    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(member="Member to kick", reason="Why they are being kicked")
    async def kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ):
        moderator = await self._require_moderator(interaction)
        if moderator is None or interaction.guild is None:
            return

        if not moderator.guild_permissions.kick_members and not moderator.guild_permissions.administrator:
            await interaction.response.send_message("You need Kick Members to use this command.", ephemeral=True)
            return

        if not await self._validate_moderation_target(
            interaction,
            member,
            action_name="kick",
            require_bot_permission="kick_members",
        ):
            return

        kick_reason = (reason or "No reason provided.").strip()

        async def action() -> str:
            try:
                await member.send(f"You were kicked from {interaction.guild.name}: {kick_reason}")
            except Exception:
                pass
            await member.kick(reason=f"{interaction.user}: {kick_reason}")
            return f"{member} has been kicked."

        await self._confirm_or_run(
            interaction,
            f"Kick {member.mention}?\nReason: {kick_reason}",
            action,
        )

    @app_commands.command(name="ban", description="Ban a member from the server")
    @app_commands.describe(
        member="Member to ban",
        reason="Why they are being banned",
        delete_message_days="Delete up to this many days of their recent messages",
    )
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
        delete_message_days: app_commands.Range[int, 0, 7] = 0,
    ):
        moderator = await self._require_moderator(interaction)
        if moderator is None or interaction.guild is None:
            return

        if not moderator.guild_permissions.ban_members and not moderator.guild_permissions.administrator:
            await interaction.response.send_message("You need Ban Members to use this command.", ephemeral=True)
            return

        if not await self._validate_moderation_target(
            interaction,
            member,
            action_name="ban",
            require_bot_permission="ban_members",
        ):
            return

        ban_reason = (reason or "No reason provided.").strip()

        async def action() -> str:
            try:
                await member.send(f"You were banned from {interaction.guild.name}: {ban_reason}")
            except Exception:
                pass
            await interaction.guild.ban(
                member,
                reason=f"{interaction.user}: {ban_reason}",
                delete_message_days=int(delete_message_days),
            )
            return f"{member} has been banned."

        await self._confirm_or_run(
            interaction,
            f"Ban {member.mention}?\nReason: {ban_reason}",
            action,
        )

    @serverguard.command(name="enableall", description="Enable every ServerGuard protection at once")
    async def serverguard_enable_all(
        self,
        interaction: discord.Interaction,
        duration_minutes: app_commands.Range[int, 1, MAX_DURATION_MINUTES] | None = None,
    ):
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return

        if not await self._ensure_bot_permissions(
            interaction,
            manage_messages=True,
            kick_members=True,
            manage_channels=True,
        ):
            return

        await self.bot.server_defense.enable_all(
            interaction.guild.id,
            duration_minutes=duration_minutes,
            actor=interaction.user,
            reason="ServerGuard enable all",
        )
        suffix = f" for `{duration_minutes}` minutes" if duration_minutes else ""
        await interaction.response.send_message(
            f"ServerGuard enabled every protection{suffix}. Armed: {self._all_features_summary(interaction.guild.id)}.",
            ephemeral=True,
        )

    @serverguard.command(name="disableall", description="Disable every ServerGuard protection at once")
    async def serverguard_disable_all(self, interaction: discord.Interaction):
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return

        await self.bot.server_defense.disable_all(
            interaction.guild.id,
            actor=interaction.user,
            reason="ServerGuard disable all",
        )
        await interaction.response.send_message(
            "ServerGuard protections disabled.",
            ephemeral=True,
        )

    @serverguard.command(name="status", description="View which ServerGuard protections are armed")
    async def serverguard_status(self, interaction: discord.Interaction):
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return

        embed = discord.Embed(title="ServerGuard", color=discord.Color.blurple())
        embed.add_field(
            name="Armed protections",
            value=self._all_features_summary(interaction.guild.id),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerDefense(bot))
