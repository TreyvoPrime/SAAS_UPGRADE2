import asyncio
import os
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from core.access import CommandAccessManager
from core.command_controls import CommandControlStore
from core.command_logs import CommandLogStore
from core.greetings import GreetingsManager, GreetingsStore
from core.server_defense import ServerDefenseManager, ServerDefenseStore
from core.warnings import WarningStore
from dashboard.app import DashboardServer, resolve_dashboard_base_url, resolve_dashboard_host, resolve_dashboard_port


env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

TOKEN = os.getenv("DISCORD_TOKEN")
APP_ID = os.getenv("DISCORD_APP_ID") or os.getenv("DISCORD_CLIENT_ID")

if not TOKEN:
    raise ValueError("DISCORD_TOKEN missing in .env")


class ServerCoreTree(app_commands.CommandTree):
    def __init__(self, client: commands.Bot):
        super().__init__(client)
        self.access_manager: CommandAccessManager = client.access_manager

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.access_manager.enforce(interaction)


class ServerCoreBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        intents.moderation = True
        intents.voice_states = True

        self.command_controls = CommandControlStore()
        self.command_logs = CommandLogStore()
        self.access_manager = CommandAccessManager(self.command_controls, self.command_logs)
        self.greetings_store = GreetingsStore()
        self.greetings = GreetingsManager(self, self.greetings_store)
        self.server_defense_store = ServerDefenseStore()
        self.server_defense = ServerDefenseManager(self, self.server_defense_store)
        self.warning_store = WarningStore()
        self._server_defense_initialized = False
        self.runtime_loop = None

        super().__init__(
            command_prefix="!",
            intents=intents,
            application_id=int(APP_ID) if APP_ID else None,
            tree_cls=ServerCoreTree,
        )

        self.dashboard = DashboardServer(self, self.command_controls, self.command_logs)

    async def setup_hook(self) -> None:
        self.runtime_loop = asyncio.get_running_loop()
        await self.load_modules()
        await self.dashboard.start()

        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} slash commands")
        except Exception as error:
            print("Sync failed:", error)

    async def close(self) -> None:
        await self.dashboard.stop()
        await super().close()

    async def load_modules(self) -> None:
        modules_path = Path("modules")
        premium_path = modules_path / "premium"

        if not modules_path.exists():
            print("No modules folder found.")
            return

        for filename in os.listdir(modules_path):
            full_path = modules_path / filename
            if full_path.is_file() and filename.endswith(".py") and filename != "__init__.py":
                try:
                    await self.load_extension(f"modules.{filename[:-3]}")
                    print(f"Loaded module: {filename}")
                except Exception as error:
                    print(f"Failed to load {filename}: {error}")

        if premium_path.exists():
            for filename in os.listdir(premium_path):
                full_path = premium_path / filename
                if full_path.is_file() and filename.endswith(".py") and filename != "__init__.py":
                    try:
                        await self.load_extension(f"modules.premium.{filename[:-3]}")
                        print(f"Loaded premium module: {filename}")
                    except Exception as error:
                        print(f"Failed to load premium module {filename}: {error}")
        else:
            print("No premium folder found.")


bot = ServerCoreBot()


@bot.event
async def on_ready():
    if not bot._server_defense_initialized:
        await bot.server_defense.initialize()
        bot._server_defense_initialized = True

    try:
        synced_count = 0
        for guild in bot.guilds:
            synced = await bot.tree.sync(guild=guild)
            synced_count += len(synced)
        if bot.guilds:
            print(f"Per-guild synced commands across {len(bot.guilds)} guild(s): {synced_count}")
    except Exception as error:
        print("Per-guild sync failed:", error)

    dashboard_host = resolve_dashboard_host()
    dashboard_port = resolve_dashboard_port()
    dashboard_url = resolve_dashboard_base_url(dashboard_host, dashboard_port)
    print(f"Logged in as {bot.user} ({bot.user.id})")
    print(f"Dashboard available at {dashboard_url}")
    print("Voice states intent:", bot.intents.voice_states)
    print("Moderation intent:", bot.intents.moderation)
    print("Members intent:", bot.intents.members)
    print("Message content intent:", bot.intents.message_content)


@bot.event
async def on_app_command_completion(interaction: discord.Interaction, command: app_commands.Command):
    bot.access_manager.log_success(interaction)


@bot.event
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    bot.access_manager.log_error(interaction, error)
    if interaction.response.is_done():
        return
    await interaction.response.send_message(
        "Something went wrong while running that command.",
        ephemeral=True,
    )

if __name__ == "__main__":
    bot.run(TOKEN)
