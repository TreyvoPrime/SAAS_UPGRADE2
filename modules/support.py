from __future__ import annotations

import io
import re

import discord
from discord import app_commands
from discord.ext import commands

from core.tickets import DEFAULT_ISSUE_TYPES


SUPPORT_CATEGORY_NAME = "ServerCore Support"


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or "member"


class SupportTicketView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="servercore:support:close",
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("This button only works inside a ticket channel.", ephemeral=True)
            return

        ticket_store = getattr(self.bot, "ticket_store", None)
        if ticket_store is None:
            await interaction.response.send_message("Ticket storage is unavailable right now.", ephemeral=True)
            return

        ticket = ticket_store.get_ticket(interaction.guild.id, interaction.channel.id)
        if ticket is None or ticket.get("status") != "open":
            await interaction.response.send_message("This channel is not tracked as an open ticket anymore.", ephemeral=True)
            return

        requester_id = int(ticket.get("requester_id", 0))
        is_requester = interaction.user.id == requester_id
        is_staff = False
        if isinstance(interaction.user, discord.Member):
            permissions = interaction.user.guild_permissions
            is_staff = (
                permissions.administrator
                or permissions.manage_guild
                or permissions.manage_channels
                or permissions.manage_messages
            )

        if not (is_requester or is_staff):
            await interaction.response.send_message("Only the requester or server staff can close this ticket.", ephemeral=True)
            return

        ticket_store.close_ticket(interaction.guild.id, interaction.channel.id)
        await interaction.response.send_message("Closing this ticket in 5 seconds...", ephemeral=True)
        try:
            await interaction.channel.send("This support ticket has been closed.")
        except Exception:
            pass
        try:
            category = interaction.channel.category
            await interaction.channel.delete(reason=f"Support ticket closed by {interaction.user}")
            support_cog = self.bot.get_cog("Support")
            if support_cog is not None:
                await support_cog._log_support_event(
                    interaction.guild,
                    title="Support Ticket Closed",
                    description=f"{interaction.channel.name} was closed.",
                    user_name=str(interaction.user),
                    channel_name=interaction.channel.name,
                    fields=[("Closed By", str(interaction.user), False)],
                )
            if support_cog is not None and category is not None:
                await support_cog._delete_empty_ticket_category(interaction.guild)
        except Exception:
            await interaction.followup.send("I couldn't delete the ticket channel. Check my channel permissions.", ephemeral=True)


class Support(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _log_support_event(
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
                color=discord.Color.blurple(),
                user_name=user_name,
                channel_name=channel_name,
                fields=fields,
            )

    async def _require_support_editor(self, interaction: discord.Interaction) -> discord.Member | None:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works inside a server.", ephemeral=True)
            return None
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("I couldn't verify your server permissions.", ephemeral=True)
            return None

        permissions = interaction.user.guild_permissions
        if not (
            permissions.administrator
            or permissions.manage_guild
            or permissions.manage_channels
            or permissions.manage_messages
            or permissions.moderate_members
        ):
            await interaction.response.send_message(
                "You need server staff permissions to edit support issue types.",
                ephemeral=True,
            )
            return None
        return interaction.user

    async def _require_ticket_staff(self, interaction: discord.Interaction) -> discord.Member | None:
        return await self._require_support_editor(interaction)

    async def _require_ticket_channel(self, interaction: discord.Interaction) -> tuple[discord.TextChannel, dict] | tuple[None, None]:
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Use this inside a support ticket channel.", ephemeral=True)
            return None, None
        ticket_store = getattr(self.bot, "ticket_store", None)
        if ticket_store is None:
            await interaction.response.send_message("Ticket support is unavailable right now.", ephemeral=True)
            return None, None
        ticket = ticket_store.get_ticket(interaction.guild.id, interaction.channel.id)
        if ticket is None:
            await interaction.response.send_message("This channel is not tracked as a ServerCore ticket.", ephemeral=True)
            return None, None
        return interaction.channel, ticket

    def _issue_types(self, guild_id: int) -> list[str]:
        ticket_store = getattr(self.bot, "ticket_store", None)
        if ticket_store is None:
            return list(DEFAULT_ISSUE_TYPES)
        return ticket_store.get_issue_types(guild_id)

    @staticmethod
    def _match_issue_type(value: str, issue_types: list[str]) -> str | None:
        desired = value.strip().casefold()
        for issue_type in issue_types:
            if issue_type.casefold() == desired:
                return issue_type
        return None

    async def _ensure_ticket_category(self, guild: discord.Guild) -> discord.CategoryChannel:
        assert getattr(self.bot, "ticket_store", None) is not None

        category_id = self.bot.ticket_store.get_support_category_id(guild.id)
        if category_id:
            category = guild.get_channel(category_id)
            if isinstance(category, discord.CategoryChannel):
                return category

        existing = discord.utils.get(guild.categories, name=SUPPORT_CATEGORY_NAME)
        if existing is not None:
            self.bot.ticket_store.set_support_category_id(guild.id, existing.id)
            return existing

        category = await guild.create_category(
            SUPPORT_CATEGORY_NAME,
            reason="Create support ticket category",
        )
        self.bot.ticket_store.set_support_category_id(guild.id, category.id)
        return category

    async def _delete_empty_ticket_category(self, guild: discord.Guild) -> None:
        assert getattr(self.bot, "ticket_store", None) is not None

        category_id = self.bot.ticket_store.get_support_category_id(guild.id)
        if not category_id:
            return

        category = guild.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            self.bot.ticket_store.set_support_category_id(guild.id, None)
            return

        if category.channels:
            return

        try:
            await category.delete(reason="ServerCore support category cleanup")
        except Exception:
            return

        self.bot.ticket_store.set_support_category_id(guild.id, None)

    async def _build_transcript(self, channel: discord.TextChannel) -> tuple[str, discord.File]:
        lines: list[str] = [f"Transcript for #{channel.name}", ""]
        async for message in channel.history(limit=200, oldest_first=True):
            created = message.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            content = message.content or ""
            attachments = ", ".join(attachment.url for attachment in message.attachments)
            if attachments:
                content = f"{content}\nAttachments: {attachments}".strip()
            lines.append(f"[{created}] {message.author}: {content}")
        transcript = "\n".join(lines)[:180000]
        filename = f"{channel.name}-transcript.txt"
        return transcript, discord.File(io.BytesIO(transcript.encode("utf-8")), filename=filename)

    @staticmethod
    def _staff_roles(guild: discord.Guild) -> list[discord.Role]:
        staff_roles: list[discord.Role] = []
        for role in guild.roles:
            if role.is_default():
                continue
            permissions = role.permissions
            if (
                permissions.administrator
                or permissions.manage_guild
                or permissions.manage_channels
                or permissions.manage_messages
                or permissions.moderate_members
            ):
                staff_roles.append(role)
        return staff_roles

    async def _ensure_bot_permissions(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        me = guild.me if guild else None
        if guild is None or me is None:
            await interaction.response.send_message("I couldn't verify my server permissions right now.", ephemeral=True)
            return False

        missing: list[str] = []
        for permission_name in ("manage_channels", "view_channel", "send_messages", "embed_links"):
            if not getattr(me.guild_permissions, permission_name, False):
                missing.append(permission_name.replace("_", " ").title())

        if missing:
            await interaction.response.send_message(
                f"I need these permissions first: {', '.join(missing)}.",
                ephemeral=True,
            )
            return False
        return True

    issue = app_commands.Group(
        name="ticketissue",
        description="Manage which support issue types members can choose",
    )

    @app_commands.command(name="ticketclaim", description="Claim the current support ticket")
    async def ticketclaim(self, interaction: discord.Interaction) -> None:
        staff = await self._require_ticket_staff(interaction)
        channel, ticket = await self._require_ticket_channel(interaction)
        if staff is None or channel is None or ticket is None or interaction.guild is None:
            return
        updated = self.bot.ticket_store.claim_ticket(
            interaction.guild.id,
            channel.id,
            staff_id=interaction.user.id,
            staff_name=str(interaction.user),
        )
        await interaction.response.send_message(f"You claimed this ticket as {interaction.user.mention}.", ephemeral=True)
        try:
            await channel.send(f"{interaction.user.mention} is now handling this ticket.")
        except Exception:
            pass
        await self._log_support_event(
            interaction.guild,
            title="Support Ticket Claimed",
            description=f"{channel.mention} was claimed by {interaction.user.mention}.",
            user_name=str(interaction.user),
            channel_name=channel.name,
            fields=[("Priority", updated.get("priority", "normal").title(), True)],
        )

    @app_commands.command(name="ticketpriority", description="Set the priority for the current support ticket")
    @app_commands.choices(priority=[
        app_commands.Choice(name="low", value="low"),
        app_commands.Choice(name="normal", value="normal"),
        app_commands.Choice(name="high", value="high"),
        app_commands.Choice(name="urgent", value="urgent"),
    ])
    async def ticketpriority(self, interaction: discord.Interaction, priority: app_commands.Choice[str]) -> None:
        staff = await self._require_ticket_staff(interaction)
        channel, ticket = await self._require_ticket_channel(interaction)
        if staff is None or channel is None or ticket is None or interaction.guild is None:
            return
        updated = self.bot.ticket_store.set_priority(interaction.guild.id, channel.id, priority.value)
        await interaction.response.send_message(f"Ticket priority set to `{priority.value}`.", ephemeral=True)
        try:
            await channel.send(f"Ticket priority updated to **{priority.value.title()}**.")
        except Exception:
            pass
        await self._log_support_event(
            interaction.guild,
            title="Support Ticket Priority Updated",
            description=f"{channel.mention} is now marked `{priority.value}`.",
            user_name=str(interaction.user),
            channel_name=channel.name,
        )

    @app_commands.command(name="tickettranscript", description="Generate a transcript for the current support ticket")
    async def tickettranscript(self, interaction: discord.Interaction) -> None:
        staff = await self._require_ticket_staff(interaction)
        channel, ticket = await self._require_ticket_channel(interaction)
        if staff is None or channel is None or ticket is None or interaction.guild is None:
            return
        _, transcript_file = await self._build_transcript(channel)
        await interaction.response.send_message("Transcript generated for this ticket.", ephemeral=True, file=transcript_file)

    @app_commands.command(name="closeticket", description="Close the current support ticket with an optional reason")
    async def closeticket(self, interaction: discord.Interaction, reason: app_commands.Range[str, 3, 300] | None = None) -> None:
        channel, ticket = await self._require_ticket_channel(interaction)
        if channel is None or ticket is None or interaction.guild is None:
            return

        requester_id = int(ticket.get("requester_id", 0))
        is_requester = interaction.user.id == requester_id
        staff = await self._require_support_editor(interaction)
        if not is_requester and staff is None:
            return

        _, transcript_file = await self._build_transcript(channel)
        updated = self.bot.ticket_store.close_ticket_with_reason(interaction.guild.id, channel.id, reason=reason)
        await interaction.response.send_message("Closing this ticket in 5 seconds...", ephemeral=True)
        audit_cog = self.bot.get_cog("AuditLogCog")
        if audit_cog is not None and hasattr(audit_cog, "get_log_channel"):
            log_channel = audit_cog.get_log_channel(interaction.guild)
            if log_channel is not None:
                try:
                    await log_channel.send(
                        content=f"Ticket transcript for #{channel.name}\nClose reason: {reason or 'No reason provided.'}",
                        file=transcript_file,
                    )
                except Exception:
                    pass
        try:
            await channel.send(f"This support ticket has been closed. Reason: {reason or 'No reason provided.'}")
        except Exception:
            pass
        await self._log_support_event(
            interaction.guild,
            title="Support Ticket Closed",
            description=f"{channel.name} was closed.",
            user_name=str(interaction.user),
            channel_name=channel.name,
            fields=[
                ("Priority", str((updated or ticket).get("priority", "normal")).title(), True),
                ("Reason", reason or "No reason provided.", False),
            ],
        )
        try:
            await channel.delete(reason=f"Support ticket closed by {interaction.user}")
            await self._delete_empty_ticket_category(interaction.guild)
        except Exception:
            await interaction.followup.send("I couldn't delete the ticket channel. Check my channel permissions.", ephemeral=True)

    @app_commands.command(name="ticket", description="Open a private support ticket for your issue")
    @app_commands.describe(
        issue_type="What kind of help you need",
        details="Describe the issue so the team knows how to help",
    )
    async def ticket(
        self,
        interaction: discord.Interaction,
        issue_type: app_commands.Range[str, 3, 80],
        details: app_commands.Range[str, 15, 1200],
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command only works inside a server.", ephemeral=True)
            return

        if not await self._ensure_bot_permissions(interaction):
            return

        ticket_store = getattr(self.bot, "ticket_store", None)
        if ticket_store is None:
            await interaction.response.send_message("Ticket support is unavailable right now.", ephemeral=True)
            return

        available_issue_types = self._issue_types(interaction.guild.id)
        selected_issue_type = self._match_issue_type(issue_type, available_issue_types)
        if selected_issue_type is None:
            await interaction.response.send_message(
                "That support category is not available in this server right now. Pick one of the listed ticket options.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        existing_channel_id = ticket_store.get_open_ticket_channel(interaction.guild.id, interaction.user.id)
        if existing_channel_id:
            existing_channel = interaction.guild.get_channel(existing_channel_id)
            if isinstance(existing_channel, discord.TextChannel):
                await interaction.followup.send(
                    f"You already have an open support ticket: {existing_channel.mention}",
                    ephemeral=True,
                )
                return
            ticket_store.clear_open_ticket(interaction.guild.id, interaction.user.id)

        category = await self._ensure_ticket_category(interaction.guild)
        ticket_number = ticket_store.next_ticket_number(interaction.guild.id)
        channel_name = f"ticket-{_slugify(interaction.user.display_name)}-{ticket_number:04d}"

        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
                embed_links=True,
                attach_files=True,
            ),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                add_reactions=True,
            ),
        }

        for role in self._staff_roles(interaction.guild):
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
                attach_files=True,
                embed_links=True,
                add_reactions=True,
            )

        try:
            ticket_channel = await interaction.guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                topic=f"ServerCore support ticket for {interaction.user} ({interaction.user.id}) | {selected_issue_type}",
                reason=f"Support ticket opened by {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.followup.send("I couldn't create the support ticket channel. Check my channel permissions.", ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.followup.send("I couldn't open a support ticket right now. Please try again in a moment.", ephemeral=True)
            return

        ticket_store.register_ticket(
            interaction.guild.id,
            channel_id=ticket_channel.id,
            requester_id=interaction.user.id,
            issue_type=selected_issue_type,
            description=details,
        )

        embed = discord.Embed(
            title="New Support Ticket",
            description="A team member will be with you shortly.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Requester", value=interaction.user.mention, inline=False)
        embed.add_field(name="Category", value=selected_issue_type, inline=True)
        embed.add_field(name="Ticket Number", value=f"#{ticket_number:04d}", inline=True)
        embed.add_field(name="Issue Details", value=details, inline=False)
        embed.set_footer(text="Use the button below when this ticket is resolved.")

        staff_mentions = [role.mention for role in self._staff_roles(interaction.guild)]
        intro = " ".join(staff_mentions[:3]).strip() or interaction.user.mention

        try:
            await ticket_channel.send(
                content=f"{intro}\nSupport request opened by {interaction.user.mention}.",
                embed=embed,
                view=SupportTicketView(self.bot),
            )
        except Exception:
            pass

        await self._log_support_event(
            interaction.guild,
            title="Support Ticket Opened",
            description=f"{interaction.user.mention} opened a support ticket.",
            user_name=str(interaction.user),
            channel_name=ticket_channel.name,
            fields=[
                ("Category", selected_issue_type, True),
                ("Ticket", ticket_channel.mention, True),
            ],
        )

        await interaction.followup.send(
            f"Your support ticket is ready: {ticket_channel.mention}",
            ephemeral=True,
        )

    @ticket.autocomplete("issue_type")
    async def ticket_issue_type_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        guild_id = interaction.guild.id if interaction.guild else None
        issue_types = self._issue_types(guild_id) if guild_id else list(DEFAULT_ISSUE_TYPES)
        query = current.strip().casefold()
        matches = [
            issue_type for issue_type in issue_types
            if not query or query in issue_type.casefold()
        ]
        return [app_commands.Choice(name=item, value=item) for item in matches[:25]]

    @issue.command(name="add", description="Add a support issue type members can choose")
    @app_commands.describe(name="The new issue type members should see in /ticket")
    async def issue_add(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 3, 80],
    ) -> None:
        member = await self._require_support_editor(interaction)
        if member is None or interaction.guild is None:
            return

        ticket_store = getattr(self.bot, "ticket_store", None)
        assert ticket_store is not None
        updated = ticket_store.add_issue_type(interaction.guild.id, name)
        await self._log_support_event(
            interaction.guild,
            title="Support Issue Added",
            description=f"`{name.strip()}` was added to the ticket issue list.",
            user_name=str(interaction.user),
            channel_name="Support Settings",
        )
        await interaction.response.send_message(
            f"Added `{name.strip()}` to the support issue list.\nAvailable issues: {', '.join(updated)}",
            ephemeral=True,
        )

    @issue.command(name="remove", description="Remove a support issue type from /ticket")
    @app_commands.describe(name="The issue type to remove")
    async def issue_remove(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 3, 80],
    ) -> None:
        member = await self._require_support_editor(interaction)
        if member is None or interaction.guild is None:
            return

        ticket_store = getattr(self.bot, "ticket_store", None)
        assert ticket_store is not None
        available = ticket_store.get_issue_types(interaction.guild.id)
        matched = self._match_issue_type(name, available)
        if matched is None:
            await interaction.response.send_message(
                "That issue type is not in this server's support list.",
                ephemeral=True,
            )
            return

        updated = ticket_store.remove_issue_type(interaction.guild.id, matched)
        await self._log_support_event(
            interaction.guild,
            title="Support Issue Removed",
            description=f"`{matched}` was removed from the ticket issue list.",
            user_name=str(interaction.user),
            channel_name="Support Settings",
        )
        await interaction.response.send_message(
            f"Removed `{matched}` from the support issue list.\nAvailable issues: {', '.join(updated)}",
            ephemeral=True,
        )

    @issue.command(name="list", description="View the support issue types members can choose")
    async def issue_list(self, interaction: discord.Interaction) -> None:
        member = await self._require_support_editor(interaction)
        if member is None or interaction.guild is None:
            return

        issue_types = self._issue_types(interaction.guild.id)
        embed = discord.Embed(
            title="Support Issue Types",
            description="\n".join(f"- {issue_type}" for issue_type in issue_types),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @issue_remove.autocomplete("name")
    async def issue_remove_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        guild_id = interaction.guild.id if interaction.guild else None
        issue_types = self._issue_types(guild_id) if guild_id else list(DEFAULT_ISSUE_TYPES)
        query = current.strip().casefold()
        matches = [
            issue_type for issue_type in issue_types
            if not query or query in issue_type.casefold()
        ]
        return [app_commands.Choice(name=item, value=item) for item in matches[:25]]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Support(bot))
    bot.add_view(SupportTicketView(bot))
