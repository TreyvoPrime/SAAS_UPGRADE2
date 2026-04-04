from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.access import CommandAccessManager
from core.billing import BillingStore
from core.command_controls import CommandControlStore
from core.command_logs import CommandLogStore
from core import storage
from core.storage import read_json, run_storage_maintenance, write_json
from modules.autoresponder import AutoResponder, FREE_AUTORESPONDER_LIMIT
from modules.premium.customcomands import CustomCommands, FREE_CUSTOM_COMMAND_LIMIT
from modules.reactionroles import ReactionRoles, FREE_PANEL_LIMIT


class FakePermissions:
    def __init__(self, **values):
        defaults = {
            "administrator": False,
            "manage_guild": False,
            "manage_messages": False,
            "moderate_members": False,
            "kick_members": False,
            "ban_members": False,
        }
        defaults.update(values)
        for key, value in defaults.items():
            setattr(self, key, value)


class FakeRole:
    def __init__(self, role_id: int, name: str | None = None):
        self.id = role_id
        self.name = name or f"Role {role_id}"
        self.mention = f"@{self.name}"


class FakeMember:
    def __init__(self, member_id: int, *, roles: list[int] | None = None, administrator: bool = False):
        self.id = member_id
        self.roles = [FakeRole(role_id) for role_id in (roles or [])]
        self.guild_permissions = FakePermissions(administrator=administrator)

    def __str__(self) -> str:
        return f"Member#{self.id}"


class FakeGuild:
    def __init__(self, guild_id: int, name: str, *, roles: list[FakeRole] | None = None):
        self.id = guild_id
        self.name = name
        self._roles = {role.id: role for role in (roles or [])}

    def get_role(self, role_id: int):
        return self._roles.get(role_id)


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content: str, *, ephemeral: bool = False) -> None:
        self.messages.append({"content": content, "ephemeral": ephemeral})
        self._done = True


class FakeCommand:
    def __init__(self, qualified_name: str):
        self.qualified_name = qualified_name


class FakeInteraction:
    def __init__(self, *, guild: FakeGuild, user: FakeMember, command_name: str):
        self.guild = guild
        self.user = user
        self.command = FakeCommand(command_name)
        self.response = FakeResponse()
        self.channel = None


class CommandAccessManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.controls = CommandControlStore(root / "command_controls.json")
        self.logs = CommandLogStore(root / "command_logs.json")
        self.manager = CommandAccessManager(self.controls, self.logs)
        self.guild = FakeGuild(1001, "Audit Guild", roles=[FakeRole(777)])
        self.member_patch = mock.patch("core.access.discord.Member", FakeMember)
        self.member_patch.start()

    async def asyncTearDown(self) -> None:
        self.member_patch.stop()
        self.tempdir.cleanup()

    async def test_disabled_command_is_blocked(self) -> None:
        self.controls.set_enabled(self.guild.id, "warn", False)
        interaction = FakeInteraction(
            guild=self.guild,
            user=FakeMember(2001, roles=[10]),
            command_name="warn",
        )

        allowed = await self.manager.enforce(interaction)

        self.assertFalse(allowed)
        self.assertEqual(interaction.response.messages[0]["content"], "This command is disabled for this server right now.")

    async def test_administrator_bypasses_role_restrictions(self) -> None:
        self.controls.set_roles(self.guild.id, "warn", [999], restrict_to_roles=True)
        interaction = FakeInteraction(
            guild=self.guild,
            user=FakeMember(2002, roles=[10], administrator=True),
            command_name="warn",
        )

        allowed = await self.manager.enforce(interaction)

        self.assertTrue(allowed)
        self.assertEqual(interaction.response.messages, [])

    async def test_explicit_role_restriction_requires_selected_role(self) -> None:
        self.controls.set_roles(self.guild.id, "warn", [777], restrict_to_roles=True)
        blocked_interaction = FakeInteraction(
            guild=self.guild,
            user=FakeMember(2003, roles=[10]),
            command_name="warn",
        )
        allowed_interaction = FakeInteraction(
            guild=self.guild,
            user=FakeMember(2004, roles=[777]),
            command_name="warn",
        )

        blocked = await self.manager.enforce(blocked_interaction)
        allowed = await self.manager.enforce(allowed_interaction)

        self.assertFalse(blocked)
        self.assertTrue(allowed)
        self.assertEqual(
            blocked_interaction.response.messages[0]["content"],
            "You do not have access to this command right now. Allowed roles: @Role 777.",
        )

    async def test_no_roles_selected_with_explicit_restriction_blocks_everyone_except_admins(self) -> None:
        self.controls.set_roles(self.guild.id, "ticketclaim", [], restrict_to_roles=True)
        interaction = FakeInteraction(
            guild=self.guild,
            user=FakeMember(2005, roles=[10]),
            command_name="ticketclaim",
        )

        allowed = await self.manager.enforce(interaction)

        self.assertFalse(allowed)
        self.assertEqual(
            interaction.response.messages[0]["content"],
            "Your roles are not allowed to use this command. Ask a server admin to update Command Access.",
        )

    async def test_role_restriction_message_lists_allowed_roles(self) -> None:
        guild = FakeGuild(1002, "Role Guild", roles=[FakeRole(321, "Moderator"), FakeRole(654, "Helper")])
        self.controls.set_roles(guild.id, "ban", [321, 654], restrict_to_roles=True)
        interaction = FakeInteraction(
            guild=guild,
            user=FakeMember(2006, roles=[10]),
            command_name="ban",
        )

        allowed = await self.manager.enforce(interaction)

        self.assertFalse(allowed)
        self.assertEqual(
            interaction.response.messages[0]["content"],
            "You do not have access to this command right now. Allowed roles: @Moderator, @Helper.",
        )


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        storage._reset_storage_backend_cache()

    def tearDown(self) -> None:
        storage._reset_storage_backend_cache()
        self.tempdir.cleanup()

    def test_read_json_returns_default_and_backs_up_corrupt_file(self) -> None:
        target = self.root / "broken.json"
        target.write_text("{not-valid-json", encoding="utf-8")

        result = read_json(target, {"ok": True})

        self.assertEqual(result, {"ok": True})
        backups = list(self.root.glob("broken.corrupt-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertFalse(target.exists())

    def test_write_json_writes_valid_json_atomically(self) -> None:
        target = self.root / "nested" / "data.json"

        write_json(target, {"guilds": {"1": {"enabled": True}}})
        loaded = read_json(target, {})

        self.assertEqual(loaded["guilds"]["1"]["enabled"], True)

    def test_postgres_backend_migrates_existing_file_on_first_read(self) -> None:
        target = self.root / "dashboard_data" / "command_controls.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        original_payload = {"guilds": {"1001": {"commands": {"warn": {"enabled": False}}}}}
        target.write_text(json.dumps(original_payload), encoding="utf-8")
        fake_psycopg, documents, _ = build_fake_psycopg()

        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://servercore:test@db/servercore"}, clear=False):
            with mock.patch("core.storage._load_psycopg", return_value=fake_psycopg):
                storage._reset_storage_backend_cache()
                loaded = read_json(target, {})

        self.assertEqual(loaded, original_payload)
        self.assertEqual(documents[storage._normalize_document_key(target)], original_payload)

    def test_postgres_backend_writes_and_reads_documents_without_local_file(self) -> None:
        target = self.root / "dashboard_data" / "command_controls.json"
        payload = {"guilds": {"1001": {"commands": {"warn": {"enabled": True, "allowed_role_ids": [10]}}}}}
        fake_psycopg, documents, _ = build_fake_psycopg()

        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://servercore:test@db/servercore"}, clear=False):
            with mock.patch("core.storage._load_psycopg", return_value=fake_psycopg):
                storage._reset_storage_backend_cache()
                write_json(target, payload)
                loaded = read_json(target, {})

        self.assertEqual(loaded, payload)
        self.assertEqual(documents[storage._normalize_document_key(target)], payload)
        self.assertFalse(target.exists())

    def test_postgres_command_logs_are_retained_and_cleaned_up(self) -> None:
        fake_psycopg, _, command_logs = build_fake_psycopg()

        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://servercore:test@db/servercore"}, clear=False):
            with mock.patch("core.storage._load_psycopg", return_value=fake_psycopg):
                storage._reset_storage_backend_cache()
                logs = CommandLogStore(self.root / "dashboard_data" / "command_logs.json")
                logs.append(
                    {
                        "guild_id": 1001,
                        "guild_name": "Audit Guild",
                        "command": "warn",
                        "status": "success",
                        "timestamp": "2026-04-03T12:00:00+00:00",
                    }
                )
                logs.append(
                    {
                        "guild_id": 1001,
                        "guild_name": "Audit Guild",
                        "command": "ban",
                        "status": "success",
                        "timestamp": "2026-03-20T12:00:00+00:00",
                    }
                )
                stored_count = len(command_logs)
                listed = logs.list_for_guild(1001, limit=10)
                maintenance = run_storage_maintenance(retention_days=5)
                remaining = logs.list_for_guild(1001, limit=10)

        self.assertEqual(stored_count, 2)
        self.assertEqual([entry["command"] for entry in listed], ["warn", "ban"])
        self.assertTrue(maintenance["healthy"])
        self.assertEqual(maintenance["deleted_logs"], 1)
        self.assertEqual([entry["command"] for entry in remaining], ["warn"])

    def test_command_controls_reload_from_postgres_across_instances(self) -> None:
        target = self.root / "dashboard_data" / "command_controls.json"
        fake_psycopg, documents, _ = build_fake_psycopg()

        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://servercore:test@db/servercore"}, clear=False):
            with mock.patch("core.storage._load_psycopg", return_value=fake_psycopg):
                storage._reset_storage_backend_cache()
                first = CommandControlStore(target)
                first.set_roles_for_commands(1001, ["warn", "ban"], [10, 11], restrict_to_roles=True)
                first.set_dashboard_editor_roles(1001, [11])

                second = CommandControlStore(target)
                warn_policy = second.get_policy(1001, "warn")
                ban_policy = second.get_policy(1001, "ban")
                editors = second.get_dashboard_editor_roles(1001)

        self.assertEqual(warn_policy["allowed_role_ids"], [10, 11])
        self.assertTrue(warn_policy["restrict_to_roles"])
        self.assertEqual(ban_policy["allowed_role_ids"], [10, 11])
        self.assertEqual(editors, [11])
        self.assertIn(storage._normalize_document_key(target), documents)


class CommandControlStoreSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.controls = CommandControlStore(self.root / "command_controls.json")
        self.billing = BillingStore(self.root / "billing.json")
        self.controls.attach_billing_store(self.billing)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_purge_settings_round_trip_and_mode_validation(self) -> None:
        settings = self.controls.set_purge_settings(
            1001,
            limit=275,
            default_mode="links",
            include_pinned_default=True,
        )
        invalid_mode_settings = self.controls.set_purge_settings(1001, default_mode="not-a-mode")

        self.assertEqual(settings["limit"], 275)
        self.assertEqual(settings["default_mode"], "links")
        self.assertTrue(settings["include_pinned_default"])
        self.assertEqual(invalid_mode_settings["default_mode"], self.controls.DEFAULT_PURGE_MODE)

    def test_alert_settings_round_trip_and_cooldown_clamping(self) -> None:
        settings = self.controls.set_alert_settings(
            1001,
            confirmation_enabled=False,
            skip_in_voice_default=False,
            only_offline_default=True,
            include_bots_default=True,
            cooldown_seconds=9999,
        )

        self.assertFalse(settings["confirmation_enabled"])
        self.assertFalse(settings["skip_in_voice_default"])
        self.assertTrue(settings["only_offline_default"])
        self.assertTrue(settings["include_bots_default"])
        self.assertEqual(settings["cooldown_seconds"], self.controls.MAX_ALERT_COOLDOWN_SECONDS)

    def test_subscription_tier_round_trip_and_free_clamps_purge_limit(self) -> None:
        self.controls.set_purge_settings(1001, limit=1500)
        self.assertFalse(self.controls.is_premium_enabled(1001))

        premium_tier = self.controls.set_subscription_tier(1001, "premium")
        self.controls.set_purge_settings(1001, limit=1500)
        free_tier = self.controls.set_subscription_tier(1001, "free")

        self.assertEqual(premium_tier, "premium")
        self.assertEqual(free_tier, "free")
        self.assertEqual(self.controls.get_purge_settings(1001)["limit"], self.controls.FREE_PURGE_LIMIT_CAP)

    def test_billing_ready_assignment_controls_premium_access(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "DISCORD_APP_ID": "1487599032170975292",
                "DISCORD_PREMIUM_SKU_ID": "1488888888888888888",
            },
            clear=False,
        ):
            self.controls.set_subscription_tier(1001, "premium")
            self.assertFalse(self.controls.is_premium_enabled(1001))

            self.billing.upsert_guild_entitlement(
                1001,
                entitlement_id="ent_123",
                premium_user_id=5001,
                sku_id="1488888888888888888",
            )

            self.assertTrue(self.controls.is_premium_enabled(1001))
            self.billing.clear_guild_entitlement(1001)
            self.assertFalse(self.controls.is_premium_enabled(1001))

    def test_set_roles_for_commands_updates_multiple_policies_in_one_call(self) -> None:
        result = self.controls.set_roles_for_commands(1001, ["warn", "ban"], [10], restrict_to_roles=True)

        self.assertEqual(sorted(result.keys()), ["ban", "warn"])
        self.assertEqual(self.controls.get_policy(1001, "warn")["allowed_role_ids"], [10])
        self.assertEqual(self.controls.get_policy(1001, "ban")["allowed_role_ids"], [10])

    def test_get_policies_reads_multiple_commands_in_one_snapshot(self) -> None:
        self.controls.set_enabled(1001, "warn", False)
        self.controls.set_roles_for_commands(1001, ["ban", "kick"], [10], restrict_to_roles=True)

        policies = self.controls.get_policies(1001, ["warn", "ban", "kick"])

        self.assertEqual(set(policies.keys()), {"warn", "ban", "kick"})
        self.assertFalse(policies["warn"]["enabled"])
        self.assertEqual(policies["ban"]["allowed_role_ids"], [10])
        self.assertEqual(policies["kick"]["allowed_role_ids"], [10])


class BillingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = BillingStore(self.root / "billing.json")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_guild_entitlement_round_trip(self) -> None:
        self.store.upsert_guild_entitlement(
            1001,
            entitlement_id="ent_123",
            premium_user_id=5001,
            sku_id="1488888888888888888",
            ends_at=1777777777,
        )

        assignment = self.store.get_guild_assignment(1001)

        self.assertTrue(assignment["is_active"])
        self.assertEqual(assignment["premium_user_id"], 5001)
        self.assertEqual(assignment["entitlement_id"], "ent_123")
        self.assertEqual(assignment["sku_id"], 1488888888888888888)
        self.assertIsNotNone(assignment["ends_at"])

    def test_store_url_uses_discord_app_and_sku(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "DISCORD_APP_ID": "1487599032170975292",
                "DISCORD_PREMIUM_SKU_ID": "1488888888888888888",
            },
            clear=False,
        ):
            self.assertEqual(
                self.store.store_url(),
                "https://discord.com/application-directory/1487599032170975292/store/1488888888888888888",
            )

    def test_sync_from_entitlements_activates_matching_guild_subscription(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "DISCORD_APP_ID": "1487599032170975292",
                "DISCORD_PREMIUM_SKU_ID": "1488888888888888888",
            },
            clear=False,
        ):
            entitlement = SimpleNamespace(
                id=9001,
                guild_id=1001,
                user_id=5001,
                sku_id=1488888888888888888,
                starts_at=None,
                ends_at=None,
                deleted=False,
                is_expired=lambda: False,
            )

            assignment = self.store.sync_from_entitlements(1001, [entitlement])

        self.assertTrue(assignment["is_active"])
        self.assertEqual(assignment["entitlement_id"], "9001")
        self.assertEqual(assignment["premium_user_id"], 5001)


class PremiumLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.controls = CommandControlStore(self.root / "command_controls.json")
        self.billing = BillingStore(self.root / "billing.json")
        self.controls.attach_billing_store(self.billing)
        self.bot = SimpleNamespace(command_controls=self.controls, billing_store=self.billing)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_free_tier_keeps_command_caps(self) -> None:
        autoresponder = AutoResponder(self.bot)
        reaction_roles = ReactionRoles(self.bot)
        custom_commands = CustomCommands(self.bot)

        self.assertEqual(autoresponder._limit(1001), FREE_AUTORESPONDER_LIMIT)
        self.assertEqual(reaction_roles._panel_limit(1001), FREE_PANEL_LIMIT)
        self.assertEqual(custom_commands._limit(1001), FREE_CUSTOM_COMMAND_LIMIT)

    def test_premium_tier_removes_command_caps(self) -> None:
        self.controls.set_subscription_tier(1001, "premium")
        autoresponder = AutoResponder(self.bot)
        reaction_roles = ReactionRoles(self.bot)
        custom_commands = CustomCommands(self.bot)

        self.assertIsNone(autoresponder._limit(1001))
        self.assertIsNone(reaction_roles._panel_limit(1001))
        self.assertIsNone(custom_commands._limit(1001))

class FakePsycopgCursor:
    def __init__(self, documents: dict[str, object], command_logs: list[dict[str, object]]):
        self.documents = documents
        self.command_logs = command_logs
        self._last_fetchone = None
        self._last_fetchall: list[tuple[str]] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query: str, params=None):
        normalized_query = " ".join(query.split()).strip().lower()
        params = params or ()
        if normalized_query.startswith("create table if not exists servercore_documents"):
            self._last_fetchone = None
            self._last_fetchall = []
            self.rowcount = 0
            return
        if normalized_query.startswith("create table if not exists servercore_command_logs"):
            self._last_fetchone = None
            self._last_fetchall = []
            self.rowcount = 0
            return
        if normalized_query.startswith("create index if not exists"):
            self._last_fetchone = None
            self._last_fetchall = []
            self.rowcount = 0
            return
        if normalized_query.startswith("select 1"):
            self._last_fetchone = (1,)
            self._last_fetchall = []
            self.rowcount = 1
            return
        if normalized_query.startswith("select payload::text from servercore_documents where document_key = %s"):
            payload = self.documents.get(params[0])
            self._last_fetchone = None if payload is None else (json.dumps(payload),)
            self._last_fetchall = []
            self.rowcount = 1 if payload is not None else 0
            return
        if normalized_query.startswith("insert into servercore_documents"):
            self.documents[params[0]] = json.loads(params[1])
            self._last_fetchone = None
            self._last_fetchall = []
            self.rowcount = 1
            return
        if normalized_query.startswith("insert into servercore_command_logs"):
            payload = json.loads(params[7])
            self.command_logs.append(
                {
                    "guild_id": int(params[0]),
                    "timestamp": str(params[1]),
                    "kind": params[2],
                    "status": params[3],
                    "category": params[4],
                    "actor_name": params[5],
                    "command_name": params[6],
                    "payload": payload,
                }
            )
            self._last_fetchone = None
            self._last_fetchall = []
            self.rowcount = 1
            return
        if normalized_query.startswith("select payload::text from servercore_command_logs where guild_id = %s"):
            guild_id = int(params[0])
            limit = int(params[-1])
            rows = [entry for entry in self.command_logs if int(entry["guild_id"]) == guild_id]
            rows.sort(key=lambda item: str(item.get("timestamp", "")), reverse=True)
            self._last_fetchall = [(json.dumps(entry["payload"]),) for entry in rows[:limit]]
            self._last_fetchone = None
            self.rowcount = len(self._last_fetchall)
            return
        if normalized_query.startswith("delete from servercore_command_logs where timestamp < %s"):
            cutoff = str(params[0])
            before = len(self.command_logs)
            self.command_logs[:] = [entry for entry in self.command_logs if str(entry.get("timestamp", "")) >= cutoff]
            self.rowcount = before - len(self.command_logs)
            self._last_fetchone = None
            self._last_fetchall = []
            return
        raise AssertionError(f"Unexpected SQL: {query}")

    def fetchone(self):
        return self._last_fetchone

    def fetchall(self):
        return list(self._last_fetchall)


class FakePsycopgConnection:
    def __init__(self, documents: dict[str, object], command_logs: list[dict[str, object]]):
        self.documents = documents
        self.command_logs = command_logs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return FakePsycopgCursor(self.documents, self.command_logs)

    def commit(self):
        return None


def build_fake_psycopg():
    documents: dict[str, object] = {}
    command_logs: list[dict[str, object]] = []

    class FakePsycopgModule:
        @staticmethod
        def connect(dsn, autocommit=True, connect_timeout=5):
            return FakePsycopgConnection(documents, command_logs)

    return FakePsycopgModule(), documents, command_logs


if __name__ == "__main__":
    unittest.main()
