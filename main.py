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
from core.storage import ensure_storage_ready, run_storage_maintenance, storage_backend_label
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
        self._storage_maintenance_task = None

        super().__init__(
            command_prefix="!",
            intents=intents,
            application_id=parse_application_id(APP_ID),
            tree_cls=ServerCoreTree,
        )

        self.dashboard = DashboardServer(self, self.command_controls, self.command_logs)

    def premium_sku_id(self) -> int | None:
        return self.billing_store.premium_sku_id()

    async def _enforce_premium_dependencies(self, guild_id: int, *, premium_active: bool) -> None:
        if premium_active:
            return
        for feature_name, reason in (
            ("antiraid", "Guardian requires Premium."),
            ("lockdown", "Lockdown requires Premium."),
        ):
            try:
                await self.server_defense.disable_feature(guild_id, feature_name, reason=reason)
            except Exception:
                if hasattr(self.server_defense, "store"):
                    self.server_defense.store.patch_feature(guild_id, feature_name, enabled=False, ends_at=None)
                if feature_name == "antiraid" and hasattr(self.server_defense, "reset_threat_state"):
                    self.server_defense.reset_threat_state(guild_id)

    async def sync_premium_for_guild(self, guild_id: int) -> dict:
        current = self.billing_store.get_guild_assignment(guild_id)
        sku_id = self.premium_sku_id()
        if sku_id is None or self.application_id is None:
            return current

        entitlement_match = None
        guild_ref = self.get_guild(guild_id) or discord.Object(id=guild_id)
        async for entitlement in self.entitlements(
            limit=100,
            guild=guild_ref,
            skus=[discord.Object(id=sku_id)],
            exclude_ended=True,
            exclude_deleted=True,
        ):
            if entitlement.guild_id != guild_id or entitlement.sku_id != sku_id:
                continue
            if entitlement.deleted or entitlement.is_expired():
                continue
            if entitlement_match is None:
                entitlement_match = entitlement
                continue
            current_end = entitlement_match.ends_at or discord.utils.utcnow()
            candidate_end = entitlement.ends_at or discord.utils.utcnow()
            if candidate_end >= current_end:
                entitlement_match = entitlement

        if entitlement_match is None:
            if current.get("activation_source") == "redeem_key" and current.get("is_active"):
                updated = current
            else:
                updated = self.billing_store.clear_guild_entitlement(guild_id)
        else:
            updated = self.billing_store.sync_from_entitlement(entitlement_match) or current

        await self._enforce_premium_dependencies(guild_id, premium_active=bool(updated.get("is_active")))
        return updated

    async def sync_all_premium_entitlements(self) -> None:
        sku_id = self.premium_sku_id()
        if sku_id is None or self.application_id is None:
            return

        active_by_guild: dict[int, discord.Entitlement] = {}
        async for entitlement in self.entitlements(
            limit=None,
            skus=[discord.Object(id=sku_id)],
            exclude_ended=True,
            exclude_deleted=True,
        ):
            guild_id = getattr(entitlement, "guild_id", None)
            if guild_id is None or entitlement.deleted or entitlement.is_expired():
                continue
            current = active_by_guild.get(guild_id)
            if current is None:
                active_by_guild[guild_id] = entitlement
                continue
            current_end = current.ends_at or discord.utils.utcnow()
            candidate_end = entitlement.ends_at or discord.utils.utcnow()
            if candidate_end >= current_end:
                active_by_guild[guild_id] = entitlement

        known_guild_ids = set(self.billing_store.stored_guild_ids()) | set(active_by_guild.keys())
        for guild_id, entitlement in active_by_guild.items():
            self.billing_store.sync_from_entitlement(entitlement)
        for guild_id in known_guild_ids - set(active_by_guild.keys()):
            current = self.billing_store.get_guild_assignment(guild_id)
            if current.get("activation_source") == "redeem_key" and current.get("is_active"):
                continue
            self.billing_store.clear_guild_entitlement(guild_id)
        for guild_id in known_guild_ids:
            await self._enforce_premium_dependencies(guild_id, premium_active=self.billing_store.guild_has_active_premium(guild_id))

    async def setup_hook(self) -> None:
        self.runtime_loop = asyncio.get_running_loop()
        await self.load_modules()
        await self.dashboard.start()
        if self._storage_maintenance_task is None:
            self._storage_maintenance_task = asyncio.create_task(self._storage_maintenance_loop())

        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} slash commands")
        except Exception as error:
            print("Sync failed:", error)

    async def close(self) -> None:
        await self.server_defense.stop()
        if self._storage_maintenance_task is not None:
            self._storage_maintenance_task.cancel()
            try:
                await self._storage_maintenance_task
            except asyncio.CancelledError:
                pass
            self._storage_maintenance_task = None
        await self.dashboard.stop()
        await super().close()

    async def _storage_maintenance_loop(self) -> None:
        while True:
            try:
                result = await asyncio.to_thread(run_storage_maintenance, retention_days=5)
                if result.get("backend") == "postgres" and not result.get("healthy", True):
                    print("Storage maintenance detected a PostgreSQL issue; see [storage] console output for details.")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"Storage maintenance failed: {error}")
            await asyncio.sleep(3600)

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

    try:
        await bot.sync_all_premium_entitlements()
    except Exception as error:
        print("Premium entitlement sync failed:", error)

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
    original = getattr(error, "original", error)

    if isinstance(original, PermissionError):
        message = str(original) or "You do not have permission to do that."
    elif isinstance(error, app_commands.TransformerError):
        option_name = getattr(error, "parameter", None)
        option_label = getattr(option_name, "display_name", None) or getattr(option_name, "name", None) or "one of the inputs"
        message = f"I couldn't understand {option_label}. Check that value and try again."
    elif isinstance(original, ValueError):
        message = str(original) or "That value is not valid for this command."
    elif isinstance(original, discord.Forbidden):
        message = "Discord blocked that action. Check my permissions and role position, then try again."
    elif isinstance(original, discord.HTTPException):
        message = "Discord had trouble finishing that action. Please try again in a moment."
    else:
        message = "Something went wrong while running that command. Please try again, and if it keeps happening check the bot logs."

    if interaction.response.is_done():
        try:
            await interaction.followup.send(message, ephemeral=True)
        except Exception:
            pass
        return

    await interaction.response.send_message(message, ephemeral=True)


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


@bot.event
async def on_entitlement_create(entitlement: discord.Entitlement):
    if entitlement.guild_id is None or entitlement.sku_id != bot.premium_sku_id():
        return
    bot.billing_store.sync_from_entitlement(entitlement)


@bot.event
async def on_entitlement_update(entitlement: discord.Entitlement):
    if entitlement.guild_id is None or entitlement.sku_id != bot.premium_sku_id():
        return
    if entitlement.deleted or entitlement.is_expired():
        bot.billing_store.clear_guild_entitlement(entitlement.guild_id)
        await bot._enforce_premium_dependencies(entitlement.guild_id, premium_active=False)
        return
    bot.billing_store.sync_from_entitlement(entitlement)


@bot.event
async def on_entitlement_delete(entitlement: discord.Entitlement):
    if entitlement.guild_id is None or entitlement.sku_id != bot.premium_sku_id():
        return
    bot.billing_store.clear_guild_entitlement(entitlement.guild_id)
    await bot._enforce_premium_dependencies(entitlement.guild_id, premium_active=False)


if __name__ == "__main__":
    bot.run(TOKEN)
