from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.access import CommandAccessManager
from core.billing import BillingStore
from core.command_controls import CommandControlStore
from core.command_logs import CommandLogStore
from core import storage
from core.storage import read_json, write_json


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
    def __init__(self, role_id: int):
        self.id = role_id


class FakeMember:
    def __init__(self, member_id: int, *, roles: list[int] | None = None, administrator: bool = False):
        self.id = member_id
        self.roles = [FakeRole(role_id) for role_id in (roles or [])]
        self.guild_permissions = FakePermissions(administrator=administrator)

    def __str__(self) -> str:
        return f"Member#{self.id}"


class FakeGuild:
    def __init__(self, guild_id: int, name: str):
        self.id = guild_id
        self.name = name


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
        self.guild = FakeGuild(1001, "Audit Guild")
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
        self.assertEqual(blocked_interaction.response.messages[0]["content"], "Your roles are not allowed to use this command.")

    async def test_no_roles_selected_with_explicit_restriction_blocks_everyone_except_admins(self) -> None:
        self.controls.set_roles(self.guild.id, "ticketclaim", [], restrict_to_roles=True)
        interaction = FakeInteraction(
            guild=self.guild,
            user=FakeMember(2005, roles=[10]),
            command_name="ticketclaim",
        )

        allowed = await self.manager.enforce(interaction)

        self.assertFalse(allowed)
        self.assertEqual(interaction.response.messages[0]["content"], "Your roles are not allowed to use this command.")


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
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
        fake_psycopg, documents = build_fake_psycopg()

        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://servercore:test@db/servercore"}, clear=False):
            with mock.patch("core.storage._load_psycopg", return_value=fake_psycopg):
                storage._reset_storage_backend_cache()
                loaded = read_json(target, {})

        self.assertEqual(loaded, original_payload)
        self.assertEqual(documents[storage._normalize_document_key(target)], original_payload)

    def test_postgres_backend_writes_and_reads_documents_without_local_file(self) -> None:
        target = self.root / "dashboard_data" / "command_controls.json"
        payload = {"guilds": {"1001": {"commands": {"warn": {"enabled": True, "allowed_role_ids": [10]}}}}}
        fake_psycopg, documents = build_fake_psycopg()

        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://servercore:test@db/servercore"}, clear=False):
            with mock.patch("core.storage._load_psycopg", return_value=fake_psycopg):
                storage._reset_storage_backend_cache()
                write_json(target, payload)
                loaded = read_json(target, {})

        self.assertEqual(loaded, payload)
        self.assertEqual(documents[storage._normalize_document_key(target)], payload)
        self.assertFalse(target.exists())


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
                "STRIPE_SECRET_KEY": "sk_test_123",
                "STRIPE_PREMIUM_PRICE_ID": "price_123",
                "STRIPE_WEBHOOK_SECRET": "whsec_123",
            },
            clear=False,
        ):
            self.controls.set_subscription_tier(1001, "premium")
            self.assertFalse(self.controls.is_premium_enabled(1001))

            self.billing.upsert_subscription(5001, status="active", stripe_customer_id="cus_123", stripe_subscription_id="sub_123")
            self.billing.assign_guild(1001, 5001)

            self.assertTrue(self.controls.is_premium_enabled(1001))
            self.billing.clear_subscription(5001)
            self.assertFalse(self.controls.is_premium_enabled(1001))


class BillingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = BillingStore(self.root / "billing.json")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_subscription_and_guild_assignment_round_trip(self) -> None:
        self.store.upsert_subscription(
            5001,
            stripe_customer_id="cus_123",
            stripe_subscription_id="sub_123",
            status="active",
            current_period_end=1777777777,
            cancel_at_period_end=False,
            email="owner@example.com",
        )
        self.store.assign_guild(1001, 5001)

        subscription = self.store.get_user_subscription(5001)
        assignment = self.store.get_guild_assignment(1001)

        self.assertTrue(subscription["is_active"])
        self.assertEqual(subscription["stripe_customer_id"], "cus_123")
        self.assertEqual(subscription["active_guild_ids"], [1001])
        self.assertEqual(assignment["premium_user_id"], 5001)
        self.assertTrue(assignment["is_active"])

    def test_webhook_tracking_is_idempotent(self) -> None:
        self.assertFalse(self.store.has_processed_webhook("evt_123"))
        self.store.mark_webhook_processed("evt_123")
        self.store.mark_webhook_processed("evt_123")
        self.assertTrue(self.store.has_processed_webhook("evt_123"))
        self.assertEqual(self.store.processed_webhook_ids(), ["evt_123"])

class FakePsycopgCursor:
    def __init__(self, documents: dict[str, object]):
        self.documents = documents
        self._last_fetchone = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query: str, params=None):
        normalized_query = " ".join(query.split()).strip().lower()
        params = params or ()
        if normalized_query.startswith("create table if not exists servercore_documents"):
            self._last_fetchone = None
            return
        if normalized_query.startswith("select payload::text from servercore_documents where document_key = %s"):
            payload = self.documents.get(params[0])
            self._last_fetchone = None if payload is None else (json.dumps(payload),)
            return
        if normalized_query.startswith("insert into servercore_documents"):
            self.documents[params[0]] = json.loads(params[1])
            self._last_fetchone = None
            return
        raise AssertionError(f"Unexpected SQL: {query}")

    def fetchone(self):
        return self._last_fetchone


class FakePsycopgConnection:
    def __init__(self, documents: dict[str, object]):
        self.documents = documents

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return FakePsycopgCursor(self.documents)


def build_fake_psycopg():
    documents: dict[str, object] = {}

    class FakePsycopgModule:
        @staticmethod
        def connect(dsn, autocommit=True, connect_timeout=5):
            return FakePsycopgConnection(documents)

    return FakePsycopgModule(), documents


if __name__ == "__main__":
    unittest.main()
