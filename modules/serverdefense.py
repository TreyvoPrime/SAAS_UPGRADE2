from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


MAX_DURATION_MINUTES = 10080


class ServerDefense(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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

    @antispam.command(name="enable", description="Rate-limit messages at 5 per 8 seconds")
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
                "Rate limit: 5 messages in 8 seconds.",
                "Action: Deletes overflow messages and attempts a short timeout.",
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

    @mentionguard.command(name="enable", description="Block messages with too many mentions")
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
                "Mention limit: 5 or more mentions in one message.",
                "Action: Deletes the message and attempts a short timeout.",
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


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerDefense(bot))
