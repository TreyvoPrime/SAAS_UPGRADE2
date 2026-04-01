from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.access import CommandAccessManager
from core.command_controls import CommandControlStore
from core.command_logs import CommandLogStore
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


if __name__ == "__main__":
    unittest.main()
