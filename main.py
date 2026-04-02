import asyncio
import os
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from core.access import CommandAccessManager
from core.autofeed import AutoFeedStore
from core.billing import BillingStore
from core.cases import ModerationCaseStore
from core.command_controls import CommandControlStore
from core.command_logs import CommandLogStore
from core.greetings import GreetingsManager, GreetingsStore
from core.giveaways import GiveawayStore
from core.server_defense import ServerDefenseManager, ServerDefenseStore
from core.staffnotes import StaffNoteStore
from core.storage import ensure_storage_ready, storage_backend_label
from core.selfroles import SelfRoleStore
from core.temp_roles import TempRoleStore
from core.tickets import TicketStore
from core.warnings import WarningStore
from dashboard.app import DashboardServer, resolve_dashboard_base_url, resolve_dashboard_host, resolve_dashboard_port


env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

TOKEN = os.getenv("DISCORD_TOKEN")
APP_ID = os.getenv("DISCORD_APP_ID") or os.getenv("DISCORD_CLIENT_ID")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Set it in the environment or .env before starting ServerCore.")


ensure_storage_ready()


def parse_application_id(raw_value: str | None) -> int | None:
    if raw_value in (None, ""):
        return None
    if not str(raw_value).isdigit():
        raise RuntimeError("DISCORD_APP_ID or DISCORD_CLIENT_ID must be a valid Discord application ID.")
    return int(str(raw_value))


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
        self.billing_store = BillingStore()
        self.command_controls.attach_billing_store(self.billing_store)
        self.command_logs = CommandLogStore()
        self.autofeed_store = AutoFeedStore()
        self.case_store = ModerationCaseStore()
        self.access_manager = CommandAccessManager(self.command_controls, self.command_logs)
        self.greetings_store = GreetingsStore()
        self.greetings = GreetingsManager(self, self.greetings_store)
        self.giveaway_store = GiveawayStore()
        self.server_defense_store = ServerDefenseStore()
        self.server_defense = ServerDefenseManager(self, self.server_defense_store)
        self.staff_note_store = StaffNoteStore()
        self.self_role_store = SelfRoleStore()
        self.ticket_store = TicketStore()
        self.temp_role_store = TempRoleStore()
        self.warning_store = WarningStore()
        self._server_defense_initialized = False
        self._commands_synced = False
        self.runtime_loop = None

        super().__init__(
            command_prefix="!",
            intents=intents,
            application_id=parse_application_id(APP_ID),
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
        await self.server_defense.stop()
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
        await bot.server_defense.start()
        bot._server_defense_initialized = True

    if not bot._commands_synced:
        try:
            synced_count = 0
            for guild in bot.guilds:
                synced = await bot.tree.sync(guild=guild)
                synced_count += len(synced)
            if bot.guilds:
                print(f"Per-guild synced commands across {len(bot.guilds)} guild(s): {synced_count}")
            bot._commands_synced = True
        except Exception as error:
            print("Per-guild sync failed:", error)

    dashboard_host = resolve_dashboard_host()
    dashboard_port = resolve_dashboard_port()
    dashboard_url = resolve_dashboard_base_url(dashboard_host, dashboard_port)
    print(f"Logged in as {bot.user} ({bot.user.id})")
    print(f"Dashboard available at {dashboard_url}")
    print("Persistent storage:", storage_backend_label())
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


@bot.event
async def on_message(message: discord.Message):
    if await bot.server_defense.process_message(message):
        return
    await bot.process_commands(message)


@bot.event
async def on_member_join(member: discord.Member):
    removed = await bot.server_defense.handle_member_join(member)
    if removed:
        channel = member.guild.system_channel
        if channel is not None:
            try:
                await channel.send(
                    f"Anti-join removed `{member}` while ServerGuard anti-join was active.",
                    delete_after=8,
                )
            except Exception:
                pass
        return
    autorole_role_ids = bot.command_controls.get_autorole_role_ids(member.guild.id)
    if autorole_role_ids:
        me = member.guild.me
        if me is not None and me.guild_permissions.manage_roles:
            roles_to_add = []
            for role_id in autorole_role_ids:
                role = member.guild.get_role(role_id)
                if role is None or role.is_default() or role >= me.top_role or role in member.roles:
                    continue
                roles_to_add.append(role)
            if roles_to_add:
                try:
                    await member.add_roles(*roles_to_add, reason="ServerCore setup wizard autorole")
                except Exception:
                    pass
    await bot.greetings.send_welcome(member)


@bot.event
async def on_member_remove(member: discord.Member):
    await bot.server_defense.handle_member_remove(member)
    await bot.greetings.send_leave(member)


@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.abc.User | discord.Member):
    await bot.server_defense.handle_reaction_add(reaction, user)


if __name__ == "__main__":
    bot.run(TOKEN)
