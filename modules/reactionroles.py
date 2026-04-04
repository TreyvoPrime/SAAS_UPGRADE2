from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.premium import command_limit, guild_has_premium, limit_reached_message, usage_footer
from core.storage import read_json, write_json

DATA_FILE = Path("reaction_roles.json")
FREE_PANEL_LIMIT = 3


def load_data() -> dict:
    data = read_json(DATA_FILE, {})
    return data if isinstance(data, dict) else {}


def save_data(data: dict) -> None:
    write_json(DATA_FILE, data if isinstance(data, dict) else {})


def parse_color(color_str: str) -> discord.Color:
    cleaned = color_str.strip().lower()
    named_colors = {
        "red": discord.Color.red(),
        "green": discord.Color.green(),
        "blue": discord.Color.blue(),
        "blurple": discord.Color.blurple(),
        "gold": discord.Color.gold(),
        "orange": discord.Color.orange(),
        "purple": discord.Color.purple(),
        "pink": discord.Color.magenta(),
        "teal": discord.Color.teal(),
        "dark_red": discord.Color.dark_red(),
        "dark_green": discord.Color.dark_green(),
        "dark_blue": discord.Color.dark_blue(),
        "dark_purple": discord.Color.dark_purple(),
        "dark_gold": discord.Color.dark_gold(),
        "dark_orange": discord.Color.dark_orange(),
        "grey": discord.Color.greyple(),
        "gray": discord.Color.greyple(),
    }
    if cleaned in named_colors:
        return named_colors[cleaned]
    if cleaned.startswith("#"):
        cleaned = cleaned[1:]
    try:
        return discord.Color(int(cleaned, 16))
    except Exception:
        return discord.Color.blurple()


def button_style_from_string(style: str) -> discord.ButtonStyle:
    mapping = {
        "primary": discord.ButtonStyle.primary,
        "secondary": discord.ButtonStyle.secondary,
        "success": discord.ButtonStyle.success,
        "danger": discord.ButtonStyle.danger,
    }
    return mapping.get(style.strip().lower(), discord.ButtonStyle.secondary)


class ReactionRoleButton(discord.ui.Button):
    def __init__(
        self,
        cog: "ReactionRoles",
        panel_id: str,
        role_id: int,
        label: str,
        style: discord.ButtonStyle,
        emoji: Optional[str] = None,
    ) -> None:
        super().__init__(
            label=label[:80],
            style=style,
            emoji=emoji if emoji else None,
            custom_id=f"rr:{panel_id}:{role_id}",
        )
        self.cog = cog
        self.panel_id = panel_id
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This button only works in a server.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("I couldn't verify your member info in this server.", ephemeral=True)
            return

        panel = self.cog.get_panel(interaction.guild.id, self.panel_id)
        if panel is None:
            await interaction.response.send_message("This role panel no longer exists.", ephemeral=True)
            return

        role = interaction.guild.get_role(self.role_id)
        if role is None:
            await interaction.response.send_message("That role no longer exists.", ephemeral=True)
            return

        bot_member = interaction.guild.me
        if bot_member is None:
            await interaction.response.send_message("I couldn't verify my own permissions in this server.", ephemeral=True)
            return
        if not bot_member.guild_permissions.manage_roles:
            await interaction.response.send_message("I need Manage Roles before I can give or remove roles.", ephemeral=True)
            return
        if role >= bot_member.top_role:
            await interaction.response.send_message("I can't manage that role because it is above my highest role.", ephemeral=True)
            return

        member = interaction.user
        if role in member.roles:
            try:
                await member.remove_roles(role, reason="Reaction role remove")
                await interaction.response.send_message(f"Removed {role.mention} from you.", ephemeral=True)
            except discord.Forbidden:
                await interaction.response.send_message("Discord blocked me from removing that role.", ephemeral=True)
            except discord.HTTPException:
                await interaction.response.send_message("Something went wrong while removing that role.", ephemeral=True)
            return

        if not panel.get("multi", True):
            panel_role_ids = {int(button["role_id"]) for button in panel.get("buttons", [])}
            removable_roles = [guild_role for guild_role in member.roles if guild_role.id in panel_role_ids and guild_role != role]
            if removable_roles:
                try:
                    await member.remove_roles(*removable_roles, reason="Reaction role single-select swap")
                except discord.Forbidden:
                    await interaction.response.send_message("I couldn't remove your previous role from this panel.", ephemeral=True)
                    return
                except discord.HTTPException:
                    await interaction.response.send_message("Something went wrong while swapping roles.", ephemeral=True)
                    return

        try:
            await member.add_roles(role, reason="Reaction role add")
            await interaction.response.send_message(f"Added {role.mention} to you.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("Discord blocked me from giving that role.", ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message("Something went wrong while giving that role.", ephemeral=True)


class ReactionRoleView(discord.ui.View):
    def __init__(self, cog: "ReactionRoles", guild_id: int, panel_id: str) -> None:
        super().__init__(timeout=None)
        panel = cog.get_panel(guild_id, panel_id)
        if panel is None:
            return
        for button_data in panel.get("buttons", [])[:25]:
            self.add_item(
                ReactionRoleButton(
                    cog=cog,
                    panel_id=panel_id,
                    role_id=int(button_data["role_id"]),
                    label=str(button_data["label"]),
                    style=button_style_from_string(str(button_data.get("style", "secondary"))),
                    emoji=button_data.get("emoji"),
                )
            )


class ReactionRoles(commands.Cog):
    reactionrole = app_commands.Group(
        name="reactionrole",
        description="Create and manage role panels with buttons",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = load_data()

    async def cog_load(self) -> None:
        self.register_persistent_views()

    def _refresh(self) -> None:
        self.data = load_data()

    def save(self) -> None:
        save_data(self.data)

    def ensure_guild_entry(self, guild_id: int) -> None:
        guild_key = str(guild_id)
        if guild_key not in self.data:
            self.data[guild_key] = {"panels": {}}

    def get_panels(self, guild_id: int) -> dict:
        self._refresh()
        self.ensure_guild_entry(guild_id)
        return self.data[str(guild_id)]["panels"]

    def get_panel(self, guild_id: int, panel_id: str) -> Optional[dict]:
        return self.get_panels(guild_id).get(panel_id)

    def next_panel_id(self, guild_id: int) -> str:
        panels = self.get_panels(guild_id)
        ids = [int(key) for key in panels if str(key).isdigit()]
        return str(max(ids, default=0) + 1)

    def _premium_enabled(self, guild_id: int, interaction: discord.Interaction | None = None) -> bool:
        return guild_has_premium(self.bot, guild_id, interaction)

    def _panel_limit(self, guild_id: int, interaction: discord.Interaction | None = None) -> int | None:
        return command_limit(FREE_PANEL_LIMIT, premium_enabled=self._premium_enabled(guild_id, interaction))

    def _panel_footer(self, guild_id: int, panel_count: int, interaction: discord.Interaction | None = None) -> str:
        return usage_footer(
            panel_count,
            "role panels",
            FREE_PANEL_LIMIT,
            premium_enabled=self._premium_enabled(guild_id, interaction),
        )

    async def _require_manager(self, interaction: discord.Interaction) -> discord.Member | None:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return None
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("I couldn't verify your server permissions.", ephemeral=True)
            return None
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("You need Manage Roles to use this command.", ephemeral=True)
            return None
        return interaction.user

    def make_embed_from_panel(self, guild: discord.Guild, panel: dict) -> discord.Embed:
        embed = discord.Embed(
            title=panel["title"],
            description=panel["description"] or "Choose a role below.",
            color=parse_color(str(panel.get("color", "blurple"))),
        )
        role_lines = []
        for button in panel.get("buttons", []):
            role = guild.get_role(int(button["role_id"]))
            role_text = role.mention if role else f"`Deleted Role ({button['role_id']})`"
            emoji_text = f"{button['emoji']} " if button.get("emoji") else ""
            role_lines.append(f"{emoji_text}**{button['label']}** -> {role_text}")
        embed.add_field(
            name="Roles in this panel",
            value="\n".join(role_lines[:25]) if role_lines else "No roles added yet.",
            inline=False,
        )
        mode_text = "Members can pick more than one role." if panel.get("multi", True) else "Members can only pick one role at a time."
        embed.add_field(name="Selection mode", value=mode_text, inline=False)
        embed.set_footer(text=f"Panel ID: {panel['id']}")
        return embed

    def register_persistent_views(self) -> None:
        self._refresh()
        for guild_id, guild_data in self.data.items():
            for panel_id in guild_data.get("panels", {}).keys():
                try:
                    self.bot.add_view(ReactionRoleView(self, int(guild_id), panel_id))
                except Exception:
                    pass

    @reactionrole.command(name="help", description="Learn the fastest way to set up a role panel")
    async def reactionrole_help(self, interaction: discord.Interaction) -> None:
        panel_count = len(self.get_panels(interaction.guild.id)) if interaction.guild else 0
        embed = discord.Embed(
            title="Reaction role help",
            description="Reaction role panels let members pick roles by pressing buttons.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Fastest setup",
            value="`/reactionrole quickcreate`\nCreates a panel and posts it in one command.",
            inline=False,
        )
        embed.add_field(
            name="Step by step",
            value="1. `/reactionrole create`\n2. `/reactionrole addrole`\n3. `/reactionrole send`",
            inline=False,
        )
        embed.add_field(
            name="Useful commands",
            value=(
                "`/reactionrole list` - see saved panels\n"
                "`/reactionrole preview` - test a panel before posting\n"
                "`/reactionrole mode` - switch between one role or many\n"
                "`/reactionrole delete` - remove a panel"
            ),
            inline=False,
        )
        embed.set_footer(text=self._panel_footer(interaction.guild.id, panel_count, interaction) if interaction.guild else "Reaction role panels")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @reactionrole.command(name="quickcreate", description="Create and post a role panel in one step")
    @app_commands.describe(
        channel="Where the panel should be posted",
        title="The title shown on the panel",
        description="Small description under the title",
        role1="First role",
        label1="Button text for the first role",
        role2="Second role",
        label2="Button text for the second role",
        role3="Optional third role",
        label3="Optional button text for the third role",
        multi="Allow members to choose more than one role from this panel",
        color="Embed color name or hex, like blurple or #5865F2",
    )
    async def quickcreate(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: app_commands.Range[str, 1, 100],
        description: app_commands.Range[str, 0, 1000],
        role1: discord.Role,
        label1: app_commands.Range[str, 1, 80],
        role2: discord.Role,
        label2: app_commands.Range[str, 1, 80],
        role3: Optional[discord.Role] = None,
        label3: Optional[str] = None,
        multi: bool = True,
        color: str = "blurple",
    ) -> None:
        manager = await self._require_manager(interaction)
        if manager is None or interaction.guild is None:
            return

        panels = self.get_panels(interaction.guild.id)
        limit = self._panel_limit(interaction.guild.id, interaction)
        if limit is not None and len(panels) >= limit:
            await interaction.response.send_message(limit_reached_message("reaction role panels in this server", limit), ephemeral=True)
            return

        bot_member = interaction.guild.me
        if bot_member is None:
            await interaction.response.send_message("I couldn't verify my own server role.", ephemeral=True)
            return

        roles_to_check = [role1, role2] + ([role3] if role3 is not None else [])
        for role in roles_to_check:
            if role is not None and role >= bot_member.top_role:
                await interaction.response.send_message(
                    f"I can't manage {role.mention} because it is above my highest role.",
                    ephemeral=True,
                )
                return

        if role3 is not None and not label3:
            await interaction.response.send_message("If you choose a third role, you also need to give it a label.", ephemeral=True)
            return

        panel_id = self.next_panel_id(interaction.guild.id)
        buttons = [
            {"role_id": role1.id, "label": label1, "emoji": None, "style": "secondary"},
            {"role_id": role2.id, "label": label2, "emoji": None, "style": "secondary"},
        ]
        if role3 is not None and label3:
            buttons.append({"role_id": role3.id, "label": label3, "emoji": None, "style": "secondary"})

        panels[panel_id] = {
            "id": panel_id,
            "title": title,
            "description": description,
            "color": color,
            "multi": multi,
            "buttons": buttons,
            "messages": [],
        }
        self.save()

        panel = panels[panel_id]
        view = ReactionRoleView(self, interaction.guild.id, panel_id)
        try:
            message = await channel.send(embed=self.make_embed_from_panel(interaction.guild, panel), view=view)
        except discord.Forbidden:
            del panels[panel_id]
            self.save()
            await interaction.response.send_message("I couldn't send the panel in that channel.", ephemeral=True)
            return
        except discord.HTTPException:
            del panels[panel_id]
            self.save()
            await interaction.response.send_message("Something went wrong while sending the panel.", ephemeral=True)
            return

        panel["messages"].append({"channel_id": channel.id, "message_id": message.id})
        self.save()
        await interaction.response.send_message(
            f"Created and sent your role panel in {channel.mention}.\nPanel ID: `{panel_id}`",
            ephemeral=True,
        )

    @reactionrole.command(name="create", description="Create a blank role panel")
    @app_commands.describe(
        title="The title shown on the panel",
        description="Small description under the title",
        color="Embed color name or hex, like blurple or #5865F2",
        multi="Allow members to choose more than one role from this panel",
    )
    async def create_panel(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 100],
        description: app_commands.Range[str, 0, 1000],
        color: app_commands.Range[str, 1, 20] = "blurple",
        multi: bool = True,
    ) -> None:
        manager = await self._require_manager(interaction)
        if manager is None or interaction.guild is None:
            return

        panels = self.get_panels(interaction.guild.id)
        limit = self._panel_limit(interaction.guild.id, interaction)
        if limit is not None and len(panels) >= limit:
            await interaction.response.send_message(limit_reached_message("reaction role panels in this server", limit), ephemeral=True)
            return

        panel_id = self.next_panel_id(interaction.guild.id)
        panels[panel_id] = {
            "id": panel_id,
            "title": title,
            "description": description,
            "color": color,
            "multi": multi,
            "buttons": [],
            "messages": [],
        }
        self.save()
        await interaction.response.send_message(
            f"Created a new role panel with ID `{panel_id}`.\nNext step: use `/reactionrole addrole` to add roles.",
            ephemeral=True,
        )

    @reactionrole.command(name="addrole", description="Add a role button to a panel")
    @app_commands.describe(
        panel_id="Which panel to edit",
        role="The role members should receive",
        label="The text shown on the button",
        style="Button style",
        emoji="Optional emoji for the button",
    )
    @app_commands.choices(
        style=[
            app_commands.Choice(name="Primary", value="primary"),
            app_commands.Choice(name="Secondary", value="secondary"),
            app_commands.Choice(name="Success", value="success"),
            app_commands.Choice(name="Danger", value="danger"),
        ]
    )
    async def addrole(
        self,
        interaction: discord.Interaction,
        panel_id: str,
        role: discord.Role,
        label: app_commands.Range[str, 1, 80],
        style: app_commands.Choice[str],
        emoji: Optional[str] = None,
    ) -> None:
        manager = await self._require_manager(interaction)
        if manager is None or interaction.guild is None:
            return

        panel = self.get_panel(interaction.guild.id, panel_id)
        if panel is None:
            await interaction.response.send_message("I couldn't find that panel ID. Use `/reactionrole list` to see your panels.", ephemeral=True)
            return

        bot_member = interaction.guild.me
        if bot_member is None or role >= bot_member.top_role:
            await interaction.response.send_message("I can't use that role because it is above my highest role.", ephemeral=True)
            return

        if any(int(button["role_id"]) == role.id for button in panel["buttons"]):
            await interaction.response.send_message("That role is already in this panel.", ephemeral=True)
            return
        if len(panel["buttons"]) >= 25:
            await interaction.response.send_message("This panel already has the maximum of 25 roles.", ephemeral=True)
            return

        panel["buttons"].append({"role_id": role.id, "label": label, "emoji": emoji, "style": style.value})
        self.save()
        await interaction.response.send_message(
            f"Added {role.mention} to panel `{panel_id}`.\nYou can preview it with `/reactionrole preview` or post it with `/reactionrole send`.",
            ephemeral=True,
        )

    @reactionrole.command(name="preview", description="Preview how a role panel will look")
    @app_commands.describe(panel_id="Which panel to preview")
    async def preview(self, interaction: discord.Interaction, panel_id: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        panel = self.get_panel(interaction.guild.id, panel_id)
        if panel is None:
            await interaction.response.send_message("I couldn't find that panel ID.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=self.make_embed_from_panel(interaction.guild, panel),
            view=ReactionRoleView(self, interaction.guild.id, panel_id),
            ephemeral=True,
        )

    @reactionrole.command(name="mode", description="Choose whether a panel allows one role or many")
    @app_commands.describe(
        panel_id="Which panel to edit",
        multi="Turn this on if members should be able to pick more than one role",
    )
    async def mode(self, interaction: discord.Interaction, panel_id: str, multi: bool) -> None:
        manager = await self._require_manager(interaction)
        if manager is None or interaction.guild is None:
            return
        panel = self.get_panel(interaction.guild.id, panel_id)
        if panel is None:
            await interaction.response.send_message("I couldn't find that panel ID.", ephemeral=True)
            return
        panel["multi"] = multi
        self.save()
        await interaction.response.send_message(
            f"Panel `{panel_id}` updated.\nMode: **{'Multiple roles allowed' if multi else 'Only one role allowed'}**",
            ephemeral=True,
        )

    @reactionrole.command(name="send", description="Post a saved role panel in a channel")
    @app_commands.describe(
        panel_id="Which panel to send",
        channel="Where the panel should be posted",
    )
    async def send_panel(self, interaction: discord.Interaction, panel_id: str, channel: discord.TextChannel) -> None:
        manager = await self._require_manager(interaction)
        if manager is None or interaction.guild is None:
            return
        panel = self.get_panel(interaction.guild.id, panel_id)
        if panel is None:
            await interaction.response.send_message("I couldn't find that panel ID.", ephemeral=True)
            return
        if not panel["buttons"]:
            await interaction.response.send_message("This panel has no roles yet. Add roles first with `/reactionrole addrole`.", ephemeral=True)
            return

        try:
            message = await channel.send(
                embed=self.make_embed_from_panel(interaction.guild, panel),
                view=ReactionRoleView(self, interaction.guild.id, panel_id),
            )
        except discord.Forbidden:
            await interaction.response.send_message("I couldn't send a message in that channel.", ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.response.send_message("Something went wrong while sending the panel.", ephemeral=True)
            return

        panel["messages"].append({"channel_id": channel.id, "message_id": message.id})
        self.save()
        await interaction.response.send_message(f"Sent panel `{panel_id}` to {channel.mention}.", ephemeral=True)

    @reactionrole.command(name="list", description="Show the role panels saved in this server")
    async def list_panels(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        panels = self.get_panels(interaction.guild.id)
        if not panels:
            await interaction.response.send_message(
                "You do not have any reaction role panels yet.\nTry `/reactionrole quickcreate` to make one quickly.",
                ephemeral=True,
            )
            return

        lines = []
        for panel_id, panel in panels.items():
            mode = "Multiple" if panel.get("multi", True) else "Single"
            lines.append(
                f"**{panel['title']}**\n"
                f"ID: `{panel_id}` | Roles: `{len(panel.get('buttons', []))}` | Mode: `{mode}` | Sent: `{len(panel.get('messages', []))}`"
            )

        embed = discord.Embed(
            title="Reaction role panels",
            description="\n\n".join(lines[:10]),
            color=discord.Color.blurple(),
        )
        if len(lines) > 10:
            embed.set_footer(text=f"Showing 10 of {len(lines)} panels")
        else:
            embed.set_footer(text=self._panel_footer(interaction.guild.id, len(lines)))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @reactionrole.command(name="delete", description="Delete a saved role panel")
    @app_commands.describe(panel_id="Which panel to delete")
    async def delete_panel(self, interaction: discord.Interaction, panel_id: str) -> None:
        manager = await self._require_manager(interaction)
        if manager is None or interaction.guild is None:
            return

        panels = self.get_panels(interaction.guild.id)
        if panel_id not in panels:
            await interaction.response.send_message("I couldn't find that panel ID.", ephemeral=True)
            return

        del panels[panel_id]
        self.save()
        await interaction.response.send_message(
            f"Deleted panel `{panel_id}`.\nOld messages from that panel will stop matching saved panel data.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRoles(bot))
