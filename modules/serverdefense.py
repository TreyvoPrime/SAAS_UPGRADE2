from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

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

    def _disable_buttons(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("This confirmation is not for you.", ephemeral=True)
            return

        self._disable_buttons()
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
        self._disable_buttons()
        try:
            await interaction.response.edit_message(content="Action canceled.", view=None)
        except Exception:
            await interaction.response.send_message("Action canceled.", ephemeral=True)


class ServerDefense(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _premium_enabled(self, guild_id: int) -> bool:
        controls = getattr(self.bot, "command_controls", None)
        return bool(controls and hasattr(controls, "is_premium_enabled") and controls.is_premium_enabled(guild_id))

    async def _require_premium_guardian(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return False
        if self._premium_enabled(interaction.guild.id):
            return True
        await interaction.response.send_message("Guardian is part of ServerCore Premium right now.", ephemeral=True)
        return False

    @staticmethod
    def _moderation_embed(title: str, description: str, *, color: discord.Color) -> discord.Embed:
        embed = discord.Embed(title=title, description=description, color=color)
        return embed

    @staticmethod
    def _format_timestamp(timestamp: str | None) -> str:
        if not timestamp:
            return "Unknown"
        try:
            normalized = timestamp.replace("Z", "+00:00")
            unix = int(datetime.fromisoformat(normalized).timestamp())
            return f"<t:{unix}:f> (<t:{unix}:R>)"
        except Exception:
            return timestamp

    async def _log_moderation_event(
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
                color=discord.Color.orange(),
                user_name=user_name,
                channel_name=channel_name,
                fields=fields,
            )

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
    autofilter = app_commands.Group(
        name="autofilter",
        description="Block flagged words or phrases automatically",
    )
    antiraid = app_commands.Group(
        name="guardian",
        description="Watch for raid pressure and raise the server threat level automatically",
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
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        state = self.bot.server_defense.get_dashboard_state(guild.id)[feature]
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
        armed = [
            feature.replace("mentionguard", "mention guard").replace("antiraid", "guardian")
            for feature in state
            if feature in {"linkblock", "inviteblock", "antispam", "antijoin", "mentionguard", "lockdown", "antiraid"}
            and state[feature].get("enabled")
        ]
        return ", ".join(armed) if armed else "none"

    def _moderation_settings(self, guild_id: int) -> dict:
        return self.bot.command_controls.get_moderation_settings(guild_id)

    def _create_case(
        self,
        guild_id: int,
        *,
        action: str,
        target: discord.Member,
        moderator: discord.Member,
        reason: str,
        duration_minutes: int | None = None,
        metadata: dict | None = None,
    ) -> dict:
        return self.bot.case_store.create_case(
            guild_id,
            action=action,
            target_user_id=target.id,
            target_user_name=str(target),
            moderator_id=moderator.id,
            moderator_name=str(moderator),
            reason=reason,
            duration_minutes=duration_minutes,
            metadata=metadata,
        )

    async def _confirm_or_run(
        self,
        interaction: discord.Interaction,
        prompt: str,
        action: Callable[[], Awaitable[str]],
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        settings = self._moderation_settings(guild.id)
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
        guild = interaction.guild
        moderator = interaction.user
        if guild is None or not isinstance(moderator, discord.Member):
            if not interaction.response.is_done():
                await interaction.response.send_message("I couldn't verify the server context for that action.", ephemeral=True)
            return False

        if member.id == moderator.id:
            await interaction.response.send_message(f"You can't {action_name} yourself.", ephemeral=True)
            return False
        if member.id == self.bot.user.id:
            await interaction.response.send_message(f"You can't {action_name} the bot.", ephemeral=True)
            return False
        if member.id == guild.owner_id:
            await interaction.response.send_message(f"You can't {action_name} the server owner.", ephemeral=True)
            return False
        if guild.owner_id != moderator.id and member.top_role >= moderator.top_role:
            await interaction.response.send_message(
                f"You can only {action_name} members below your top role.",
                ephemeral=True,
            )
            return False

        me = guild.me
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

    case = app_commands.Group(
        name="case",
        description="View moderation cases and add internal notes",
    )
    staffnotes = app_commands.Group(
        name="staffnotes",
        description="Keep internal staff notes on members",
    )

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

    @autofilter.command(name="enable", description="Turn on the blocked-word autofilter")
    async def autofilter_enable(
        self,
        interaction: discord.Interaction,
        duration_minutes: app_commands.Range[int, 1, MAX_DURATION_MINUTES] | None = None,
    ):
        await self._enable_feature(
            interaction,
            "autofilter",
            "AutoFilter",
            duration_minutes=duration_minutes,
            permission_requirements={"manage_messages": True, "moderate_members": True},
        )

    @autofilter.command(name="disable", description="Turn off the blocked-word autofilter")
    async def autofilter_disable(self, interaction: discord.Interaction):
        await self._disable_feature(interaction, "autofilter", "AutoFilter")

    @autofilter.command(name="status", description="View the current autofilter status and term list")
    async def autofilter_status(self, interaction: discord.Interaction):
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return
        state = self.bot.server_defense.get_dashboard_state(interaction.guild.id)["autofilter"]
        terms = state.get("filter_terms", [])
        await self._send_status(
            interaction,
            "autofilter",
            "AutoFilter",
            [
                f"Warnings: {state.get('warning_limit', 3)} before a {state.get('timeout_minutes', 60)} minute timeout.",
                f"Terms: {', '.join(terms[:12]) if terms else 'No blocked terms configured yet.'}",
            ],
        )

    @autofilter.command(name="add", description="Add a blocked word or phrase to AutoFilter")
    async def autofilter_add(self, interaction: discord.Interaction, phrase: app_commands.Range[str, 1, 80]):
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return
        state = self.bot.server_defense.get_dashboard_state(interaction.guild.id)["autofilter"]
        terms = list(state.get("filter_terms", []))
        cleaned = phrase.strip().lower()
        if cleaned not in terms:
            terms.append(cleaned)
        updated = self.bot.server_defense.update_autofilter_terms(interaction.guild.id, terms)
        await interaction.response.send_message(
            f"AutoFilter updated. It is now blocking {len(updated.get('filter_terms', []))} word(s) or phrase(s).",
            ephemeral=True,
        )

    @autofilter.command(name="remove", description="Remove a blocked word or phrase from AutoFilter")
    async def autofilter_remove(self, interaction: discord.Interaction, phrase: app_commands.Range[str, 1, 80]):
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return
        state = self.bot.server_defense.get_dashboard_state(interaction.guild.id)["autofilter"]
        cleaned = phrase.strip().lower()
        terms = [term for term in state.get("filter_terms", []) if term != cleaned]
        updated = self.bot.server_defense.update_autofilter_terms(interaction.guild.id, terms)
        await interaction.response.send_message(
            f"AutoFilter updated. It is now blocking {len(updated.get('filter_terms', []))} word(s) or phrase(s).",
            ephemeral=True,
        )

    @autofilter.command(name="list", description="List the blocked words and phrases in AutoFilter")
    async def autofilter_list(self, interaction: discord.Interaction):
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return
        state = self.bot.server_defense.get_dashboard_state(interaction.guild.id)["autofilter"]
        terms = state.get("filter_terms", [])
        if not terms:
            await interaction.response.send_message("AutoFilter has no blocked terms configured yet.", ephemeral=True)
            return
        embed = discord.Embed(title="AutoFilter terms", description="\n".join(f"- {term}" for term in terms[:50]), color=discord.Color.orange())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @antiraid.command(name="enable", description="Start Guardian threat scoring and automatic escalation")
    async def antiraid_enable(
        self,
        interaction: discord.Interaction,
        duration_minutes: app_commands.Range[int, 1, MAX_DURATION_MINUTES] | None = None,
    ):
        if not await self._require_premium_guardian(interaction):
            return
        await self._enable_feature(
            interaction,
            "antiraid",
            "Guardian",
            duration_minutes=duration_minutes,
        )

    @antiraid.command(name="disable", description="Stop Guardian threat scoring and clear the live threat state")
    async def antiraid_disable(self, interaction: discord.Interaction):
        if not await self._require_premium_guardian(interaction):
            return
        await self._disable_feature(interaction, "antiraid", "Guardian")

    @antiraid.command(name="status", description="View the live Guardian score and response tier")
    async def antiraid_status(self, interaction: discord.Interaction):
        if not await self._require_premium_guardian(interaction):
            return
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return
        threat = self.bot.server_defense.get_threat_summary(interaction.guild.id)
        extra_lines = [
            f"Threat level: {threat['level_label']}",
            f"Score: {threat['score_display']}",
            f"Response: {threat['status_copy']}",
        ]
        if threat["recent_actions"]:
            extra_lines.append(f"Automatic actions: {', '.join(threat['recent_actions'])}")
        await self._send_status(
            interaction,
            "antiraid",
            "Guardian",
            extra_lines,
        )

    @antiraid.command(name="reset", description="Clear the live Guardian score and recent signals")
    async def antiraid_reset(self, interaction: discord.Interaction):
        if not await self._require_premium_guardian(interaction):
            return
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return
        threat = self.bot.server_defense.reset_threat_state(interaction.guild.id)
        await interaction.response.send_message(
            f"Guardian score reset. Current level: {threat['level_label']} ({threat['score_display']}).",
            ephemeral=True,
        )

    @antiraid.command(name="blacklistadd", description="Add a phrase Guardian should block immediately")
    async def antiraid_blacklist_add(self, interaction: discord.Interaction, phrase: app_commands.Range[str, 3, 80]):
        if not await self._require_premium_guardian(interaction):
            return
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return
        threat = self.bot.server_defense.get_threat_summary(interaction.guild.id)
        phrases = list(threat.get("blocked_phrases", []))
        if phrase.lower() not in phrases:
            phrases.append(phrase.lower())
        updated = self.bot.server_defense.update_guardian_lists(interaction.guild.id, blocked_phrases=phrases)
        await interaction.response.send_message(f"Guardian blacklist updated. Now blocking {len(updated.get('blocked_phrases', []))} phrase(s).", ephemeral=True)

    @antiraid.command(name="blacklistremove", description="Remove a blocked Guardian phrase")
    async def antiraid_blacklist_remove(self, interaction: discord.Interaction, phrase: app_commands.Range[str, 3, 80]):
        if not await self._require_premium_guardian(interaction):
            return
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return
        threat = self.bot.server_defense.get_threat_summary(interaction.guild.id)
        phrases = [item for item in threat.get("blocked_phrases", []) if item != phrase.lower()]
        updated = self.bot.server_defense.update_guardian_lists(interaction.guild.id, blocked_phrases=phrases)
        await interaction.response.send_message(f"Guardian blacklist updated. Now blocking {len(updated.get('blocked_phrases', []))} phrase(s).", ephemeral=True)

    @antiraid.command(name="whitelistadd", description="Allow a domain through Link Block and Invite Block")
    async def antiraid_whitelist_add(self, interaction: discord.Interaction, domain: app_commands.Range[str, 3, 120]):
        if not await self._require_premium_guardian(interaction):
            return
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return
        cleaned = domain.strip().lower().replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
        threat = self.bot.server_defense.get_threat_summary(interaction.guild.id)
        domains = list(threat.get("allowed_domains", []))
        if cleaned not in domains:
            domains.append(cleaned)
        updated = self.bot.server_defense.update_guardian_lists(interaction.guild.id, allowed_domains=domains)
        await interaction.response.send_message(f"Guardian whitelist updated. Allowed domains: {', '.join(updated.get('allowed_domains', [])) or 'None'}", ephemeral=True)

    @antiraid.command(name="whitelistremove", description="Remove a domain from the Guardian allow list")
    async def antiraid_whitelist_remove(self, interaction: discord.Interaction, domain: app_commands.Range[str, 3, 120]):
        if not await self._require_premium_guardian(interaction):
            return
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return
        cleaned = domain.strip().lower().replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
        threat = self.bot.server_defense.get_threat_summary(interaction.guild.id)
        domains = [item for item in threat.get("allowed_domains", []) if item != cleaned]
        updated = self.bot.server_defense.update_guardian_lists(interaction.guild.id, allowed_domains=domains)
        await interaction.response.send_message(f"Guardian whitelist updated. Allowed domains: {', '.join(updated.get('allowed_domains', [])) or 'None'}", ephemeral=True)

    @antiraid.command(name="preset", description="Apply a Guardian tuning preset")
    @app_commands.choices(preset=[
        app_commands.Choice(name="balanced", value="balanced"),
        app_commands.Choice(name="strict", value="strict"),
        app_commands.Choice(name="emergency", value="emergency"),
    ])
    async def antiraid_preset(self, interaction: discord.Interaction, preset: app_commands.Choice[str]):
        if not await self._require_premium_guardian(interaction):
            return
        member = await self._require_admin(interaction)
        if member is None or interaction.guild is None:
            return
        updated = self.bot.server_defense.apply_guardian_preset(interaction.guild.id, preset.value)
        await interaction.response.send_message(f"Guardian preset set to `{updated.get('preset', preset.value)}`.", ephemeral=True)

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
            case = self._create_case(
                interaction.guild.id,
                action="warn",
                target=member,
                moderator=moderator,
                reason=warning_reason,
            )
            self.bot.warning_store.add_warning(
                interaction.guild.id,
                member.id,
                moderator_id=interaction.user.id,
                reason=warning_reason,
            )
            warning_count = self.bot.warning_store.warning_count(interaction.guild.id, member.id)
            dm_sent = await self._send_warning_dm(member, interaction.guild.name, warning_reason)
            await self._log_moderation_event(
                interaction.guild,
                title="Member Warned",
                description=f"{member.mention} was warned.",
                user_name=str(interaction.user),
                channel_name=getattr(interaction.channel, "name", None),
                fields=[
                    ("Case ID", f"#{case['case_id']}", True),
                    ("Reason", warning_reason, False),
                    ("Warning Count", str(warning_count), True),
                    ("DM Sent", "Yes" if dm_sent else "No", True),
                ],
            )
            status_line = "They received a private copy of the warning." if dm_sent else "I couldn't deliver the private warning message, but the warning was still saved."
            return f"{member.mention} has been warned.\nCase ID: #{case['case_id']}\nReason: {warning_reason}\nTotal warnings: {warning_count}\n{status_line}"

        await self._confirm_or_run(
            interaction,
            f"Send a warning to {member.mention}?\nReason: {warning_reason}",
            action,
        )

    @app_commands.command(name="history", description="Show a member's moderation history in one staff view")
    @app_commands.describe(member="Member whose history you want to review")
    async def history(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ):
        moderator = await self._require_moderator(interaction)
        if moderator is None or interaction.guild is None:
            return

        warnings = self.bot.warning_store.list_warnings(interaction.guild.id, member.id)
        cases = self.bot.case_store.list_user_cases(interaction.guild.id, member.id, limit=10)
        staff_notes = self.bot.staff_note_store.list_notes(interaction.guild.id, member.id)

        embed = discord.Embed(
            title=f"History for {member.display_name}",
            description=f"Staff view for {member.mention}",
            color=member.color if member.color != discord.Color.default() else discord.Color.blurple(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Warnings", value=str(len(warnings)), inline=True)
        embed.add_field(name="Cases", value=str(len(cases)), inline=True)
        embed.add_field(name="Staff Notes", value=str(len(staff_notes)), inline=True)

        timeout_until = getattr(member, "timed_out_until", None)
        embed.add_field(
            name="Current timeout",
            value=self._format_timestamp(timeout_until.isoformat()) if timeout_until else "Not timed out",
            inline=False,
        )

        if warnings:
            warning_lines = []
            for index, warning in enumerate(warnings[-5:], start=max(len(warnings) - 4, 1)):
                warning_lines.append(
                    f"#{index} • {warning.get('reason', 'No reason provided.')}\n{self._format_timestamp(warning.get('timestamp'))}"
                )
            embed.add_field(name="Recent warnings", value="\n\n".join(warning_lines)[:1024], inline=False)

        if cases:
            case_lines = []
            for case in cases[:5]:
                reason = case.get("reason") or "No reason provided."
                case_lines.append(
                    f"Case #{case['case_id']} • {case['action'].title()}\n{reason}\n{self._format_timestamp(case.get('created_at'))}"
                )
            embed.add_field(name="Recent cases", value="\n\n".join(case_lines)[:1024], inline=False)

        if staff_notes:
            note_lines = []
            for note in staff_notes[-5:]:
                note_lines.append(
                    f"Note #{note['note_id']} • {note.get('moderator_name', 'Unknown')}\n{note.get('note', '')}\n{self._format_timestamp(note.get('timestamp'))}"
                )
            embed.add_field(name="Recent staff notes", value="\n\n".join(reversed(note_lines))[:1024], inline=False)

        embed.set_footer(text="Only staff can see this history.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

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
            case = self._create_case(
                interaction.guild.id,
                action="timeout",
                target=member,
                moderator=moderator,
                reason=timeout_reason,
                duration_minutes=timeout_minutes,
            )
            await self._log_moderation_event(
                interaction.guild,
                title="Member Timed Out",
                description=f"{member.mention} was timed out.",
                user_name=str(interaction.user),
                channel_name=getattr(interaction.channel, "name", None),
                fields=[
                    ("Case ID", f"#{case['case_id']}", True),
                    ("Duration", f"{timeout_minutes} minutes", True),
                    ("Reason", timeout_reason, False),
                ],
            )
            return f"{member.mention} has been timed out for {timeout_minutes} minutes.\nCase ID: #{case['case_id']}"

        await self._confirm_or_run(
            interaction,
            f"Timeout {member.mention} for {timeout_minutes} minutes?\nReason: {timeout_reason}",
            action,
        )

    @app_commands.command(name="removetimeout", description="Remove a timeout from a member")
    @app_commands.describe(member="Member whose timeout should be removed", reason="Why the timeout is being removed")
    async def removetimeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ):
        moderator = await self._require_moderator(interaction)
        if moderator is None or interaction.guild is None:
            return

        if not await self._validate_moderation_target(
            interaction,
            member,
            action_name="remove a timeout from",
            require_bot_permission="moderate_members",
        ):
            return

        timeout_reason = (reason or "No reason provided.").strip()

        async def action() -> str:
            refreshed_member = interaction.guild.get_member(member.id) or member
            timeout_until = getattr(refreshed_member, "timed_out_until", None)
            if timeout_until is None:
                return f"{member.mention} is not currently timed out."

            await refreshed_member.timeout(
                None,
                reason=f"{interaction.user}: Removed timeout. {timeout_reason}",
            )
            await self._log_moderation_event(
                interaction.guild,
                title="Timeout Removed",
                description=f"{member.mention}'s timeout was removed.",
                user_name=str(interaction.user),
                channel_name=getattr(interaction.channel, "name", None),
                fields=[
                    ("Reason", timeout_reason, False),
                ],
            )
            return f"Removed the timeout from {member.mention}.\nReason: {timeout_reason}"

        await self._confirm_or_run(
            interaction,
            f"Remove the timeout from {member.mention}?\nReason: {timeout_reason}",
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
            case = self._create_case(
                interaction.guild.id,
                action="kick",
                target=member,
                moderator=moderator,
                reason=kick_reason,
            )
            await self._log_moderation_event(
                interaction.guild,
                title="Member Kicked",
                description=f"{member} was kicked from the server.",
                user_name=str(interaction.user),
                channel_name=getattr(interaction.channel, "name", None),
                fields=[
                    ("Case ID", f"#{case['case_id']}", True),
                    ("Reason", kick_reason, False),
                ],
            )
            return f"{member} has been kicked.\nCase ID: #{case['case_id']}"

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
            case = self._create_case(
                interaction.guild.id,
                action="ban",
                target=member,
                moderator=moderator,
                reason=ban_reason,
                metadata={"delete_message_days": int(delete_message_days)},
            )
            await self._log_moderation_event(
                interaction.guild,
                title="Member Banned",
                description=f"{member} was banned from the server.",
                user_name=str(interaction.user),
                channel_name=getattr(interaction.channel, "name", None),
                fields=[
                    ("Case ID", f"#{case['case_id']}", True),
                    ("Reason", ban_reason, False),
                    ("Delete Message Days", str(int(delete_message_days)), True),
                ],
            )
            return f"{member} has been banned.\nCase ID: #{case['case_id']}"

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
        threat = self.bot.server_defense.get_threat_summary(interaction.guild.id)
        embed.add_field(
            name="Armed protections",
            value=self._all_features_summary(interaction.guild.id),
            inline=False,
        )
        embed.add_field(
            name="Threat level",
            value=f"{threat['level_label']} ({threat['score_display']})",
            inline=False,
        )
        embed.add_field(
            name="Guardian",
            value=threat["status_copy"],
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @case.command(name="view", description="View a moderation case by its case ID")
    async def case_view(
        self,
        interaction: discord.Interaction,
        case_id: app_commands.Range[int, 1, 1000000],
    ):
        moderator = await self._require_moderator(interaction)
        if moderator is None or interaction.guild is None:
            return

        case = self.bot.case_store.get_case(interaction.guild.id, int(case_id))
        if case is None:
            await interaction.response.send_message("I couldn't find that case in this server.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"Case #{case['case_id']}",
            color=discord.Color.blurple(),
            description=f"{case['action'].title()} case for `{case['target_user_name']}`",
        )
        embed.add_field(name="Moderator", value=case["moderator_name"], inline=True)
        embed.add_field(name="Target", value=case["target_user_name"], inline=True)
        embed.add_field(name="Created", value=case["created_at"], inline=False)
        embed.add_field(name="Reason", value=case["reason"], inline=False)
        if case.get("duration_minutes"):
            embed.add_field(name="Duration", value=f"{case['duration_minutes']} minutes", inline=True)

        notes = case.get("notes", [])
        if notes:
            preview = []
            for note in notes[-5:]:
                preview.append(f"[{note['type']}] {note['actor_name']}: {note['body']}")
            embed.add_field(name="History", value="\n".join(preview)[:1024], inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @case.command(name="note", description="Add an internal note to a moderation case")
    async def case_note(
        self,
        interaction: discord.Interaction,
        case_id: app_commands.Range[int, 1, 1000000],
        note: app_commands.Range[str, 3, 500],
    ):
        moderator = await self._require_moderator(interaction)
        if moderator is None or interaction.guild is None:
            return

        updated = self.bot.case_store.add_note(
            interaction.guild.id,
            int(case_id),
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
            note=note,
        )
        if updated is None:
            await interaction.response.send_message("I couldn't find that case in this server.", ephemeral=True)
            return

        await self._log_moderation_event(
            interaction.guild,
            title="Case Note Added",
            description=f"Added a note to case #{case_id}.",
            user_name=str(interaction.user),
            channel_name=getattr(interaction.channel, "name", None),
            fields=[("Note", note, False)],
        )
        await interaction.response.send_message(f"Added a note to case #{case_id}.", ephemeral=True)

    @staffnotes.command(name="view", description="View the internal staff notes for a member")
    async def staffnotes_view(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ):
        moderator = await self._require_moderator(interaction)
        if moderator is None or interaction.guild is None:
            return

        notes = self.bot.staff_note_store.list_notes(interaction.guild.id, member.id)
        if not notes:
            await interaction.response.send_message(
                f"There are no staff notes saved for {member.mention}.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"Staff notes for {member.display_name}",
            description=f"{member.mention} has {len(notes)} internal note(s).",
            color=discord.Color.blurple(),
        )
        lines = []
        for note in notes[-10:]:
            lines.append(
                f"Note #{note['note_id']} • {note.get('moderator_name', 'Unknown')}\n{note.get('note', '')}\n{self._format_timestamp(note.get('timestamp'))}"
            )
        embed.add_field(name="Latest notes", value="\n\n".join(reversed(lines))[:1024], inline=False)
        embed.set_footer(text="Only staff can see these notes.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @staffnotes.command(name="add", description="Add an internal staff note to a member")
    async def staffnotes_add(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        note: app_commands.Range[str, 3, 500],
    ):
        moderator = await self._require_moderator(interaction)
        if moderator is None or interaction.guild is None:
            return

        saved = self.bot.staff_note_store.add_note(
            interaction.guild.id,
            member.id,
            moderator_id=interaction.user.id,
            moderator_name=str(interaction.user),
            note=note,
        )
        await self._log_moderation_event(
            interaction.guild,
            title="Staff Note Added",
            description=f"Added an internal note for {member.mention}.",
            user_name=str(interaction.user),
            channel_name=getattr(interaction.channel, "name", None),
            fields=[
                ("Note ID", f"#{saved['note_id']}", True),
                ("Note", note, False),
            ],
        )
        await interaction.response.send_message(
            f"Saved staff note #{saved['note_id']} for {member.mention}.",
            ephemeral=True,
        )

    @staffnotes.command(name="remove", description="Remove a saved internal staff note from a member")
    async def staffnotes_remove(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        note_id: app_commands.Range[int, 1, 1000000],
    ):
        moderator = await self._require_moderator(interaction)
        if moderator is None or interaction.guild is None:
            return

        removed = self.bot.staff_note_store.remove_note(interaction.guild.id, member.id, int(note_id))
        if not removed:
            await interaction.response.send_message(
                f"I couldn't find note #{note_id} for {member.mention}.",
                ephemeral=True,
            )
            return

        await self._log_moderation_event(
            interaction.guild,
            title="Staff Note Removed",
            description=f"Removed internal note #{note_id} from {member.mention}.",
            user_name=str(interaction.user),
            channel_name=getattr(interaction.channel, "name", None),
        )
        await interaction.response.send_message(
            f"Removed staff note #{note_id} from {member.mention}.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerDefense(bot))
