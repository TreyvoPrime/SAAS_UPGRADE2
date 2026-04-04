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
        description="Website tools for managing this server",
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

    @dashboard.command(name="open", description="Get the website link to manage this server")
    async def open_dashboard(self, interaction: discord.Interaction):
        member = await self._require_manage_server(interaction)
        if member is None or interaction.guild is None:
            return

        dashboard_link = self._dashboard_url(interaction.guild.id)
        embed = discord.Embed(
            title="Manage This Server",
            description=f"[Open the ServerCore website for **{interaction.guild.name}**]({dashboard_link})",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Website link", value=dashboard_link, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @dashboard.command(name="overview", description="See a quick summary of this server's settings")
    async def overview(self, interaction: discord.Interaction):
        member = await self._require_manage_server(interaction)
        if member is None or interaction.guild is None:
            return

        commands_list = build_command_catalog(self.bot)
        controls = self.bot.access_manager.controls
        policies = controls.get_policies(interaction.guild.id, [command["name"] for command in commands_list])
        disabled_count = sum(1 for policy in policies.values() if not policy["enabled"])
        restricted_count = sum(1 for policy in policies.values() if policy["allowed_role_ids"])

        embed = discord.Embed(
            title=f"{interaction.guild.name} Settings Summary",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Commands available", value=str(len(commands_list)), inline=True)
        embed.add_field(name="Turned off", value=str(disabled_count), inline=True)
        embed.add_field(name="Role-limited", value=str(restricted_count), inline=True)
        embed.add_field(name="Manage online", value=self._dashboard_url(interaction.guild.id), inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @dashboard.command(name="command", description="Check whether a command is on and who can use it")
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
            title=f"Command Check: /{command['name']}",
            description=command["description"],
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Section", value=command["module"], inline=True)
        embed.add_field(name="Plan", value=command["tier"], inline=True)
        embed.add_field(name="Turned on", value="Yes" if policy["enabled"] else "No", inline=True)
        embed.add_field(
            name="Who can use it",
            value=", ".join(role_mentions) if role_mentions else "Anyone who already passes the command's built-in checks",
            inline=False,
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @dashboard.command(name="toggle", description="Turn a command on or off from Discord")
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
            f"/{command['name']} is now {'turned on' if policy['enabled'] else 'turned off'}.",
            ephemeral=True,
        )

    @dashboard.command(name="role", description="Choose which roles can use a command")
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
        access_summary = ", ".join(role_mentions) if role_mentions else "Anyone who passes the command's built-in checks"

        await interaction.response.send_message(
            (
                f"Updated access for /{command['name']}.\n"
                f"Who can use it: {access_summary}"
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(DashboardCommands(bot))
