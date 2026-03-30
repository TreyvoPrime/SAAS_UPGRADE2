import discord
from discord import app_commands
from discord.ext import commands

from core.command_catalog import build_command_catalog
from dashboard.app import resolve_dashboard_base_url, resolve_dashboard_host, resolve_dashboard_port


class DashboardCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    dashboard = app_commands.Group(
        name="dashboard",
        description="Open and manage the ServerCore dashboard for this server",
    )

    def _dashboard_url(self, guild_id: int) -> str:
        host = resolve_dashboard_host()
        port = resolve_dashboard_port()
        base_url = resolve_dashboard_base_url(host, port)
        return f"{base_url.rstrip('/')}/dashboard/{guild_id}"

    def _find_command(self, command_name: str) -> dict | None:
        normalized = command_name.strip().lower()
        for command in build_command_catalog(self.bot):
            if command["name"].lower() == normalized:
                return command
        return None

    async def _require_manage_server(self, interaction: discord.Interaction) -> discord.Member | None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command only works in a server.",
                ephemeral=True,
            )
            return None

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "I could not verify your member permissions.",
                ephemeral=True,
            )
            return None

        if not (
            interaction.user.guild_permissions.manage_guild
            or interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "You need Manage Server or Administrator to use dashboard commands.",
                ephemeral=True,
            )
            return None

        return interaction.user

    async def _command_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild is None:
            return []

        query = current.lower().strip()
        choices = []
        for command in build_command_catalog(self.bot):
            if query and query not in command["name"].lower():
                continue
            choices.append(app_commands.Choice(name=f"/{command['name']}", value=command["name"]))
            if len(choices) >= 25:
                break
        return choices

    @dashboard.command(name="open", description="Get the dashboard link for this server")
    async def open_dashboard(self, interaction: discord.Interaction):
        member = await self._require_manage_server(interaction)
        if member is None or interaction.guild is None:
            return

        dashboard_link = self._dashboard_url(interaction.guild.id)
        embed = discord.Embed(
            title="Server Dashboard",
            description=f"[Open the dashboard for **{interaction.guild.name}**]({dashboard_link})",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Direct link", value=dashboard_link, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @dashboard.command(name="overview", description="View dashboard status for this server")
    async def overview(self, interaction: discord.Interaction):
        member = await self._require_manage_server(interaction)
        if member is None or interaction.guild is None:
            return

        commands_list = build_command_catalog(self.bot)
        controls = self.bot.access_manager.controls
        disabled_count = 0
        restricted_count = 0
        for command in commands_list:
            policy = controls.get_policy(interaction.guild.id, command["name"])
            if not policy["enabled"]:
                disabled_count += 1
            if policy["allowed_role_ids"]:
                restricted_count += 1

        embed = discord.Embed(
            title=f"{interaction.guild.name} Dashboard Overview",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Commands", value=str(len(commands_list)), inline=True)
        embed.add_field(name="Disabled", value=str(disabled_count), inline=True)
        embed.add_field(name="Restricted", value=str(restricted_count), inline=True)
        embed.add_field(name="Dashboard", value=self._dashboard_url(interaction.guild.id), inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @dashboard.command(name="command", description="View dashboard permissions for a specific command")
    @app_commands.autocomplete(command_name=_command_autocomplete)
    async def command_info(self, interaction: discord.Interaction, command_name: str):
        member = await self._require_manage_server(interaction)
        if member is None or interaction.guild is None:
            return

        command = self._find_command(command_name)
        if command is None:
            await interaction.response.send_message("I couldn't find that command.", ephemeral=True)
            return

        policy = self.bot.access_manager.controls.get_policy(interaction.guild.id, command["name"])
        role_mentions = [
            interaction.guild.get_role(role_id).mention
            for role_id in policy["allowed_role_ids"]
            if interaction.guild.get_role(role_id) is not None
        ]

        embed = discord.Embed(
            title=f"/{command['name']}",
            description=command["description"],
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Module", value=command["module"], inline=True)
        embed.add_field(name="Tier", value=command["tier"], inline=True)
        embed.add_field(name="Enabled", value="Yes" if policy["enabled"] else "No", inline=True)
        embed.add_field(
            name="Allowed roles",
            value=", ".join(role_mentions) if role_mentions else "All roles that pass the command's built-in checks",
            inline=False,
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @dashboard.command(name="toggle", description="Enable or disable a command from Discord")
    @app_commands.autocomplete(command_name=_command_autocomplete)
    async def toggle_command(self, interaction: discord.Interaction, command_name: str, enabled: bool):
        member = await self._require_manage_server(interaction)
        if member is None or interaction.guild is None:
            return

        command = self._find_command(command_name)
        if command is None:
            await interaction.response.send_message("I couldn't find that command.", ephemeral=True)
            return

        policy = self.bot.access_manager.controls.set_enabled(interaction.guild.id, command["name"], enabled)
        await interaction.response.send_message(
            f"/{command['name']} is now {'enabled' if policy['enabled'] else 'disabled'}.",
            ephemeral=True,
        )

    @dashboard.command(name="role", description="Add or remove an allowed role for a command")
    @app_commands.autocomplete(command_name=_command_autocomplete)
    @app_commands.choices(mode=[
        app_commands.Choice(name="add", value="add"),
        app_commands.Choice(name="remove", value="remove"),
        app_commands.Choice(name="clear all", value="clear"),
    ])
    async def role_access(
        self,
        interaction: discord.Interaction,
        command_name: str,
        mode: app_commands.Choice[str],
        role: discord.Role | None = None,
    ):
        member = await self._require_manage_server(interaction)
        if member is None or interaction.guild is None:
            return

        command = self._find_command(command_name)
        if command is None:
            await interaction.response.send_message("I couldn't find that command.", ephemeral=True)
            return

        policy = self.bot.access_manager.controls.get_policy(interaction.guild.id, command["name"])
        allowed_role_ids = set(policy["allowed_role_ids"])

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

        updated = self.bot.access_manager.controls.set_roles(
            interaction.guild.id,
            command["name"],
            sorted(allowed_role_ids),
        )

        role_mentions = [
            interaction.guild.get_role(role_id).mention
            for role_id in updated["allowed_role_ids"]
            if interaction.guild.get_role(role_id) is not None
        ]

        await interaction.response.send_message(
            (
                f"Updated roles for /{command['name']}.\n"
                f"Allowed roles: {', '.join(role_mentions) if role_mentions else 'All roles that pass built-in checks'}"
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(DashboardCommands(bot))
