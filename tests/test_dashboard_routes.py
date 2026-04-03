from __future__ import annotations

import os
import tempfile
import unittest
import re
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

from core.access import CommandAccessManager
from core.autofeed import AutoFeedStore
from core.billing import BillingStore
from core.cases import ModerationCaseStore
from core.command_controls import CommandControlStore
from core.command_logs import CommandLogStore
from core.giveaways import GiveawayStore
from core.greetings import GreetingsManager, GreetingsStore
from core.server_defense import ServerDefenseStore
from core.tickets import TicketStore
from dashboard.app import create_dashboard_app


class MockHTTPResponse:
    def __init__(self, payload, *, status_code: int = 200, headers: dict | None = None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.request = httpx.Request("GET", "https://discord.com/api/mock")

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )


class FakePermissions:
    def __init__(self, **values):
        defaults = {
            "administrator": False,
            "manage_guild": False,
            "manage_channels": False,
            "manage_messages": False,
            "manage_roles": False,
            "moderate_members": False,
            "kick_members": False,
            "ban_members": False,
        }
        defaults.update(values)
        for key, value in defaults.items():
            setattr(self, key, value)


class FakeRole:
    def __init__(self, role_id: int, name: str, position: int, *, is_default: bool = False):
        self.id = role_id
        self.name = name
        self.position = position
        self._is_default = is_default
        self.permissions = FakePermissions(
            administrator=position >= 10,
            manage_guild=position >= 10,
            manage_channels=position >= 9,
            manage_messages=position >= 9,
            moderate_members=position >= 9,
        )

    def is_default(self) -> bool:
        return self._is_default

    @property
    def mention(self) -> str:
        return f"<@&{self.id}>"


class FakeTextChannel:
    def __init__(self, channel_id: int, name: str, position: int):
        self.id = channel_id
        self.name = name
        self.position = position

    @property
    def mention(self) -> str:
        return f"<#{self.id}>"


class FakeMember:
    def __init__(self, member_id: int, name: str, guild: "FakeGuild", *, role_ids: list[int], permissions: FakePermissions):
        self.id = member_id
        self.name = name
        self.display_name = name
        self.guild = guild
        self.guild_permissions = permissions
        self.roles = [guild.get_role(role_id) for role_id in role_ids if guild.get_role(role_id) is not None]
        self.top_role = max(self.roles, key=lambda role: role.position) if self.roles else guild.default_role

    def __str__(self) -> str:
        return self.name


class FakeGuild:
    def __init__(self):
        self.id = 1485162416059842582
        self.name = "Audit Guild"
        self.owner_id = 5001
        self.icon = None
        self.default_role = FakeRole(1, "@everyone", 0, is_default=True)
        self.roles = [
            self.default_role,
            FakeRole(10, "Moderators", 10),
            FakeRole(11, "Helpers", 8),
            FakeRole(99, "ServerCore Bot", 50),
        ]
        self.text_channels = [
            FakeTextChannel(3001, "general", 1),
            FakeTextChannel(3002, "support", 2),
        ]
        self.member_count = 2
        self.members: list[FakeMember] = []
        self.me: FakeMember | None = None

    def get_role(self, role_id: int) -> FakeRole | None:
        for role in self.roles:
            if role.id == role_id:
                return role
        return None

    def get_channel(self, channel_id: int):
        for channel in self.text_channels:
            if channel.id == channel_id:
                return channel
        return None

    def get_member(self, member_id: int) -> FakeMember | None:
        for member in self.members:
            if member.id == member_id:
                return member
        return None

    async def fetch_member(self, member_id: int) -> FakeMember:
        member = self.get_member(member_id)
        if member is None:
            raise LookupError("member not found")
        return member


class FakeDefenseManager:
    def __init__(self, store: ServerDefenseStore):
        self.store = store

    def get_dashboard_state(self, guild_id: int) -> dict:
        state = self.store.get_all(guild_id)
        if not getattr(self, "controls").is_premium_enabled(guild_id):
            state["antiraid"]["enabled"] = False
        return state

    def build_dashboard_state(self, guild_id: int, role_lookup: dict[int, str] | None = None) -> dict:
        raw = self.store.get_all(guild_id)
        cards = []
        for name in ("linkblock", "inviteblock", "antispam", "antijoin", "mentionguard", "autofilter", "lockdown", "antiraid"):
            state = raw[name]
            locked = name == "antiraid" and not self.controls.is_premium_enabled(guild_id)
            cards.append(
                {
                    "name": name,
                    "title": name.title(),
                    "enabled": bool(state.get("enabled")) and not locked,
                    "locked": locked,
                    "duration_label": "Until disabled",
                    "status_label": "Locked on Free" if locked else ("Enabled" if state.get("enabled") else "Disabled"),
                    "remaining_label": "Locked on Free" if locked else "No timer",
                }
            )
        return {
            "cards": cards,
            "lockdown_allowed_role_names": [],
            "guardian": {"status_label": "Offline"},
            "active_count": len([card for card in cards if card["enabled"]]),
            "timed_count": 0,
            "lockdown_role_count": 0,
            "autofilter_terms": [],
            "autofilter_timeout_minutes": 60,
            "threat": {"level_label": "Offline" if not self.controls.is_premium_enabled(guild_id) else "Offline"},
        }

    async def set_defense(self, guild_id: int, defense_name: str, *, enabled: bool, duration_minutes: int | None = None) -> dict:
        if defense_name == "antiraid" and enabled and not self.controls.is_premium_enabled(guild_id):
            raise PermissionError("Guardian is part of ServerCore Premium right now.")
        return self.store.patch_feature(guild_id, defense_name, enabled=enabled)

    async def disable_feature(self, guild_id: int, feature: str, *, reason: str | None = None) -> dict:
        return self.store.patch_feature(guild_id, feature, enabled=False)

    def reset_threat_state(self, guild_id: int) -> dict:
        return {"level_label": "Offline", "score_display": "0/100"}


class FakeBot:
    def __init__(self, root: Path):
        self.guild = FakeGuild()
        owner = FakeMember(
            5001,
            "OwnerUser",
            self.guild,
            role_ids=[1, 10],
            permissions=FakePermissions(
                administrator=True,
                manage_guild=True,
                manage_channels=True,
                manage_messages=True,
                manage_roles=True,
                moderate_members=True,
            ),
        )
        editor = FakeMember(
            5002,
            "EditorUser",
            self.guild,
            role_ids=[1, 11],
            permissions=FakePermissions(),
        )
        bot_member = FakeMember(
            9001,
            "ServerCore",
            self.guild,
            role_ids=[1, 99],
            permissions=FakePermissions(
                administrator=True,
                manage_guild=True,
                manage_channels=True,
                manage_messages=True,
                manage_roles=True,
                moderate_members=True,
                kick_members=True,
                ban_members=True,
            ),
        )
        self.guild.members = [owner, editor, bot_member]
        self.guild.me = bot_member
        self.guilds = [self.guild]
        self.user = SimpleNamespace(name="ServerCore", id=1487599032170975292)
        self.tree = SimpleNamespace(get_commands=lambda guild=None: [])
        self.command_controls = CommandControlStore(root / "command_controls.json")
        self.billing_store = BillingStore(root / "billing.json")
        self.command_controls.attach_billing_store(self.billing_store)
        self.command_logs = CommandLogStore(root / "command_logs.json")
        self.access_manager = CommandAccessManager(self.command_controls, self.command_logs)
        self.ticket_store = TicketStore(root / "tickets.json")
        self.greetings_store = GreetingsStore(root / "greetings.json")
        self.greetings = GreetingsManager(self, self.greetings_store)
        self.giveaway_store = GiveawayStore(root / "giveaways.json")
        self.autofeed_store = AutoFeedStore(root / "autofeed.json")
        self.case_store = ModerationCaseStore(root / "cases.json")
        self.server_defense_store = ServerDefenseStore(root / "server_defense.json")
        self.server_defense = FakeDefenseManager(self.server_defense_store)
        self.server_defense.controls = self.command_controls
        self.runtime_loop = None
        self.synced_premium_guilds: list[int] = []

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        return self.guild if self.guild.id == guild_id else None

    def get_cog(self, name: str):
        return None

    async def sync_premium_for_guild(self, guild_id: int):
        self.synced_premium_guilds.append(guild_id)
        return self.billing_store.get_guild_assignment(guild_id)

    async def sync_all_premium_entitlements(self):
        return None


class DashboardRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "DASHBOARD_BASE_URL": "http://testserver",
                "DASHBOARD_SECRET_KEY": "x" * 48,
                "DISCORD_CLIENT_ID": "1487599032170975292",
                "DISCORD_CLIENT_SECRET": "super-secret-client-secret",
            },
            clear=False,
        )
        self.env_patch.start()
        self.bot = FakeBot(self.root)
        self.app = create_dashboard_app(self.bot)
        self.catalog_patch = mock.patch(
            "dashboard.app.build_command_catalog",
            return_value=[
                {"name": "warn", "description": "Warn a member", "module": "ServerGuard", "tier": "Free"},
                {"name": "ticketclaim", "description": "Claim a ticket", "module": "Support", "tier": "Free"},
                {"name": "poll", "description": "Create a poll", "module": "Polls", "tier": "Free"},
            ],
        )
        self.catalog_patch.start()
        self.client = TestClient(self.app, base_url="http://testserver")

    def tearDown(self) -> None:
        self.client.close()
        self.catalog_patch.stop()
        self.env_patch.stop()
        self.tempdir.cleanup()

    def _http_patches(self, *, user_id: str = "5001", owner: bool = True, permissions: int = 0x20):
        async def mock_post(_client, url, data=None, headers=None, **kwargs):
            return MockHTTPResponse({"access_token": "discord-oauth-token"})

        async def mock_get(_client, url, headers=None, **kwargs):
            if url.endswith("/users/@me"):
                return MockHTTPResponse(
                    {
                        "id": user_id,
                        "username": "OwnerUser" if user_id == "5001" else "EditorUser",
                        "global_name": "Owner User" if user_id == "5001" else "Editor User",
                        "avatar": None,
                    }
                )
            if url.endswith("/users/@me/guilds"):
                return MockHTTPResponse(
                    [
                        {
                            "id": str(self.bot.guild.id),
                            "name": self.bot.guild.name,
                            "permissions": str(permissions),
                            "owner": owner,
                            "icon": None,
                        }
                    ]
                )
            return MockHTTPResponse({}, status_code=404)

        return (
            mock.patch.object(httpx.AsyncClient, "post", new=mock_post),
            mock.patch.object(httpx.AsyncClient, "get", new=mock_get),
        )

    def _authenticate(self, *, user_id: str = "5001", owner: bool = True, permissions: int = 0x20) -> None:
        post_patch, get_patch = self._http_patches(user_id=user_id, owner=owner, permissions=permissions)
        with post_patch, get_patch:
            response = self.client.get("/login", follow_redirects=False)
            self.assertEqual(response.status_code, 302)
            state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
            callback = self.client.get(f"/auth/callback?code=test-code&state={state}", follow_redirects=False)
            self.assertEqual(callback.status_code, 302)
            self.assertEqual(callback.headers["location"], "/servers")
            servers = self.client.get("/servers")
            self.assertEqual(servers.status_code, 200)

    def test_login_and_oauth_callback_create_server_side_session(self) -> None:
        self._authenticate()

        cookie_value = self.client.cookies.get("servercore_dashboard_session", "")
        self.assertEqual(len(self.app.state.dashboard_sessions._sessions), 1)
        self.assertNotIn("discord-oauth-token", cookie_value)

        _, get_patch = self._http_patches()
        with get_patch:
            response = self.client.get("/servers")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Audit Guild", response.text)

    def test_health_and_home_routes_are_available(self) -> None:
        health = self.client.get("/health")
        home = self.client.get("/", follow_redirects=False)

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["ok"], True)
        self.assertEqual(home.status_code, 200)
        self.assertIn("ServerCore", home.text)

    def test_auth_callback_rejects_invalid_state(self) -> None:
        post_patch, get_patch = self._http_patches()
        with post_patch, get_patch:
            self.client.get("/login", follow_redirects=False)
            response = self.client.get("/auth/callback?code=test-code&state=wrong-state", follow_redirects=False)
        self.assertEqual(response.status_code, 400)

    def test_dashboard_redirects_to_setup_until_setup_is_completed(self) -> None:
        self._authenticate()

        dashboard = self.client.get(f"/dashboard/{self.bot.guild.id}", follow_redirects=False)
        setup = self.client.get(f"/dashboard/{self.bot.guild.id}/setup", follow_redirects=False)

        self.assertEqual(dashboard.status_code, 302)
        self.assertEqual(dashboard.headers["location"], f"/dashboard/{self.bot.guild.id}/setup")
        self.assertEqual(setup.status_code, 200)

    def test_logs_route_requires_authentication_and_then_returns_entries(self) -> None:
        unauthenticated = self.client.get(f"/api/guilds/{self.bot.guild.id}/logs")
        self.assertEqual(unauthenticated.status_code, 401)

        self._authenticate()
        self.bot.command_logs.append({"guild_id": self.bot.guild.id, "guild_name": self.bot.guild.name, "command": "warn", "status": "success"})
        authenticated = self.client.get(f"/api/guilds/{self.bot.guild.id}/logs")

        self.assertEqual(authenticated.status_code, 200)
        self.assertEqual(len(authenticated.json()["entries"]), 1)

    def test_command_policy_route_requires_same_origin(self) -> None:
        self._authenticate()
        response = self.client.post(
            f"/api/guilds/{self.bot.guild.id}/command-policy",
            json={"command_name": "warn", "enabled": False},
            headers={"origin": "http://evil.example"},
        )

        self.assertEqual(response.status_code, 403)

    def test_command_policy_route_persists_changes(self) -> None:
        self._authenticate()
        response = self.client.post(
            f"/api/guilds/{self.bot.guild.id}/command-policy",
            json={"command_name": "warn", "enabled": False, "allowed_role_ids": [10]},
            headers={"origin": "http://testserver"},
        )

        self.assertEqual(response.status_code, 200)
        policy = self.bot.command_controls.get_policy(self.bot.guild.id, "warn")
        self.assertFalse(policy["enabled"])
        self.assertEqual(policy["allowed_role_ids"], [10])
        self.assertTrue(policy["restrict_to_roles"])

    def test_support_settings_route_saves_issue_types_and_command_channel(self) -> None:
        self._authenticate()
        response = self.client.post(
            f"/api/guilds/{self.bot.guild.id}/support-settings",
            json={"issue_types": ["Moderation", "Appeals"], "command_channel_id": str(3002)},
            headers={"origin": "http://testserver"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.bot.ticket_store.get_issue_types(self.bot.guild.id), ["Moderation", "Appeals"])
        self.assertEqual(self.bot.ticket_store.get_support_command_channel_id(self.bot.guild.id), 3002)

    def test_greetings_route_updates_join_dm_message_and_enabled_state(self) -> None:
        self._authenticate()
        response = self.client.post(
            f"/api/guilds/{self.bot.guild.id}/greetings",
            json={
                "flow": "join_dm",
                "enabled": True,
                "message": "Welcome to {server}, {display_name}.",
            },
            headers={"origin": "http://testserver"},
        )

        self.assertEqual(response.status_code, 200)
        join_dm = self.bot.greetings_store.get_guild(self.bot.guild.id)
        self.assertTrue(join_dm["join_dm_enabled"])
        self.assertEqual(join_dm["join_dm_message"], "Welcome to {server}, {display_name}.")
        self.assertTrue(response.json()["join_dm"]["enabled"])

    def test_purge_settings_route_saves_limit_mode_and_pinned_behavior(self) -> None:
        self._authenticate()
        response = self.client.post(
            f"/api/guilds/{self.bot.guild.id}/purge-settings",
            json={
                "limit": 275,
                "default_mode": "links",
                "include_pinned_default": True,
            },
            headers={"origin": "http://testserver"},
        )

        self.assertEqual(response.status_code, 200)
        settings = self.bot.command_controls.get_purge_settings(self.bot.guild.id)
        self.assertEqual(settings["limit"], 275)
        self.assertEqual(settings["default_mode"], "links")
        self.assertTrue(settings["include_pinned_default"])
        self.assertEqual(response.json()["default_mode"], "links")

    def test_alert_settings_route_saves_role_alert_defaults(self) -> None:
        self._authenticate()
        response = self.client.post(
            f"/api/guilds/{self.bot.guild.id}/alert-settings",
            json={
                "confirmation_enabled": False,
                "skip_in_voice_default": False,
                "only_offline_default": True,
                "include_bots_default": True,
                "cooldown_seconds": 300,
            },
            headers={"origin": "http://testserver"},
        )

        self.assertEqual(response.status_code, 200)
        settings = self.bot.command_controls.get_alert_settings(self.bot.guild.id)
        self.assertFalse(settings["confirmation_enabled"])
        self.assertFalse(settings["skip_in_voice_default"])
        self.assertTrue(settings["only_offline_default"])
        self.assertTrue(settings["include_bots_default"])
        self.assertEqual(settings["cooldown_seconds"], 300)
        self.assertEqual(response.json()["cooldown_seconds"], 300)

    def test_subscription_route_switches_between_free_and_premium(self) -> None:
        self._authenticate()
        premium = self.client.post(
            f"/api/guilds/{self.bot.guild.id}/subscription",
            json={"tier": "premium"},
            headers={"origin": "http://testserver"},
        )
        self.assertEqual(premium.status_code, 200)
        self.assertTrue(self.bot.command_controls.is_premium_enabled(self.bot.guild.id))
        free = self.client.post(
            f"/api/guilds/{self.bot.guild.id}/subscription",
            json={"tier": "free"},
            headers={"origin": "http://testserver"},
        )

        self.assertEqual(free.status_code, 200)
        self.assertFalse(self.bot.command_controls.is_premium_enabled(self.bot.guild.id))

    def test_billing_page_renders_for_authenticated_user(self) -> None:
        self._authenticate()

        response = self.client.get(f"/billing?guild_id={self.bot.guild.id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(f"Premium for {self.bot.guild.name}", response.text)
        self.assertIn("How Premium turns on", response.text)
        self.assertIn("Discord billing not ready", response.text)
        self.assertIn("$2.99", response.text)
        self.assertIn("/month", response.text)

    def test_dashboard_renders_subscription_workspace_copy(self) -> None:
        self._authenticate()
        self.bot.command_controls.set_setup_wizard_completed(self.bot.guild.id, True)

        response = self.client.get(f"/dashboard/{self.bot.guild.id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Premium for this server", response.text)
        self.assertIn("Status checks run automatically while this page is open.", response.text)
        self.assertIn("Discord checkout opens in a new tab and unlocks this server automatically once the purchase is granted.", response.text)

    def test_subscription_upgrade_requires_active_discord_guild_sku_when_billing_ready(self) -> None:
        self._authenticate()

        with mock.patch.dict(
            os.environ,
            {
                "DISCORD_APP_ID": "1487599032170975292",
                "DISCORD_PREMIUM_SKU_ID": "1488888888888888888",
            },
            clear=False,
        ):
            blocked = self.client.post(
                f"/api/guilds/{self.bot.guild.id}/subscription",
                json={"tier": "premium"},
                headers={"origin": "http://testserver"},
            )

            self.assertEqual(blocked.status_code, 402)
            self.assertEqual(blocked.json()["billing_url"], f"/billing?guild_id={self.bot.guild.id}")

            self.bot.billing_store.upsert_guild_entitlement(
                self.bot.guild.id,
                entitlement_id="ent_123",
                premium_user_id=5001,
                sku_id="1488888888888888888",
            )
            allowed = self.client.post(
                f"/api/guilds/{self.bot.guild.id}/subscription",
                json={"tier": "premium"},
                headers={"origin": "http://testserver"},
            )

            self.assertEqual(allowed.status_code, 200)
            self.assertTrue(self.bot.command_controls.is_premium_enabled(self.bot.guild.id))
            self.assertEqual(self.bot.billing_store.get_guild_assignment(self.bot.guild.id)["premium_user_id"], 5001)

    def test_billing_page_shows_discord_store_link_when_sku_is_configured(self) -> None:
        self._authenticate()

        with mock.patch.dict(
            os.environ,
            {
                "DISCORD_APP_ID": "1487599032170975292",
                "DISCORD_PREMIUM_SKU_ID": "1488888888888888888",
            },
            clear=False,
        ):
            response = self.client.get(f"/billing?guild_id={self.bot.guild.id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("https://discord.com/application-directory/1487599032170975292/store/1488888888888888888", response.text)
        self.assertIn("Buy Premium in Discord", response.text)

    def test_billing_page_highlights_active_premium_for_selected_server(self) -> None:
        self._authenticate()

        with mock.patch.dict(
            os.environ,
            {
                "DISCORD_APP_ID": "1487599032170975292",
                "DISCORD_PREMIUM_SKU_ID": "1488888888888888888",
            },
            clear=False,
        ):
            self.bot.billing_store.upsert_guild_entitlement(
                self.bot.guild.id,
                entitlement_id="ent_123",
                premium_user_id=5001,
                sku_id="1488888888888888888",
            )
            response = self.client.get(f"/billing?guild_id={self.bot.guild.id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Premium is active for Audit Guild", response.text)
        self.assertIn("Refresh status", response.text)
        self.assertIn("Open billing in Discord", response.text)

    def test_premium_server_renders_toggle_for_premium_command_cards(self) -> None:
        self._authenticate()
        self.bot.command_controls.set_setup_wizard_completed(self.bot.guild.id, True)

        with mock.patch.dict(
            os.environ,
            {
                "DISCORD_APP_ID": "1487599032170975292",
                "DISCORD_PREMIUM_SKU_ID": "1488888888888888888",
            },
            clear=False,
        ):
            self.bot.billing_store.upsert_guild_entitlement(
                self.bot.guild.id,
                entitlement_id="ent_123",
                premium_user_id=5001,
                sku_id="1488888888888888888",
            )
            with mock.patch(
                "dashboard.app.build_command_catalog",
                return_value=[
                    {"name": "guardian enable", "description": "Enable Guardian", "module": "ServerGuard", "tier": "Premium"},
                ],
            ):
                response = self.client.get(f"/dashboard/{self.bot.guild.id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-command-name="guardian enable"', response.text)
        self.assertRegex(
            response.text,
            re.compile(r'<input\s+type="checkbox"\s+checked\s+data-command-toggle\s+data-command-name="guardian enable"', re.MULTILINE),
        )
        self.assertNotIn("Locked on Free. Turn Premium on above to unlock it.", response.text)

    def test_guardian_requires_premium_from_dashboard(self) -> None:
        self._authenticate()
        blocked = self.client.post(
            f"/api/guilds/{self.bot.guild.id}/defense-state",
            json={"defense_name": "antiraid", "enabled": True},
            headers={"origin": "http://testserver"},
        )
        self.client.post(
            f"/api/guilds/{self.bot.guild.id}/subscription",
            json={"tier": "premium"},
            headers={"origin": "http://testserver"},
        )
        allowed = self.client.post(
            f"/api/guilds/{self.bot.guild.id}/defense-state",
            json={"defense_name": "antiraid", "enabled": True},
            headers={"origin": "http://testserver"},
        )

        self.assertEqual(blocked.status_code, 402)
        self.assertEqual(allowed.status_code, 200)

    def test_premium_log_search_and_export_require_premium(self) -> None:
        self._authenticate()
        self.bot.command_logs.append(
            {
                "guild_id": self.bot.guild.id,
                "guild_name": self.bot.guild.name,
                "kind": "event",
                "title": "Dashboard Updated",
                "summary": "Changed support settings",
                "status": "event",
                "user_name": "OwnerUser",
                "channel_name": "Dashboard",
                "category": "dashboard",
            }
        )

        blocked = self.client.get(f"/api/guilds/{self.bot.guild.id}/logs?query=support")
        self.assertEqual(blocked.status_code, 402)

        self.client.post(
            f"/api/guilds/{self.bot.guild.id}/subscription",
            json={"tier": "premium"},
            headers={"origin": "http://testserver"},
        )
        _, get_patch = self._http_patches()
        with get_patch:
            searched = self.client.get(f"/api/guilds/{self.bot.guild.id}/logs?query=support&category=dashboard")
            exported = self.client.get(f"/api/guilds/{self.bot.guild.id}/logs/export?category=dashboard")

        self.assertEqual(searched.status_code, 200)
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(len(searched.json()["entries"]), 1)
        self.assertEqual(exported.json()["entries"][0]["category"], "dashboard")

    def test_premium_case_routes_require_premium(self) -> None:
        self._authenticate()
        self.bot.case_store.create_case(
            self.bot.guild.id,
            action="warn",
            target_user_id=5002,
            target_user_name="EditorUser",
            moderator_id=5001,
            moderator_name="OwnerUser",
            reason="Test warning",
        )

        blocked = self.client.get(f"/api/guilds/{self.bot.guild.id}/cases")
        self.assertEqual(blocked.status_code, 402)

        self.client.post(
            f"/api/guilds/{self.bot.guild.id}/subscription",
            json={"tier": "premium"},
            headers={"origin": "http://testserver"},
        )
        _, get_patch = self._http_patches()
        with get_patch:
            listing = self.client.get(f"/api/guilds/{self.bot.guild.id}/cases?query=EditorUser")
            exported = self.client.get(f"/api/guilds/{self.bot.guild.id}/cases/export?action=warn")

        self.assertEqual(listing.status_code, 200)
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(listing.json()["entries"][0]["target_user_name"], "EditorUser")
        self.assertEqual(exported.json()["entries"][0]["action"], "warn")

    def test_dashboard_editor_role_can_edit_settings_but_cannot_manage_editor_roles(self) -> None:
        self.bot.command_controls.set_dashboard_editor_roles(self.bot.guild.id, [11])
        self._authenticate(user_id="5002", owner=False, permissions=0)

        support_response = self.client.post(
            f"/api/guilds/{self.bot.guild.id}/support-settings",
            json={"issue_types": ["Reports"], "command_channel_id": str(3002)},
            headers={"origin": "http://testserver"},
        )
        editor_roles_response = self.client.post(
            f"/api/guilds/{self.bot.guild.id}/dashboard-access",
            json={"editor_role_ids": [11]},
            headers={"origin": "http://testserver"},
        )

        self.assertEqual(support_response.status_code, 200)
        self.assertEqual(editor_roles_response.status_code, 403)

    def test_setup_wizard_persists_explicit_role_restrictions(self) -> None:
        self._authenticate()
        response = self.client.post(
            f"/api/guilds/{self.bot.guild.id}/setup-wizard",
            json={
                "moderation_confirmation_enabled": True,
                "default_timeout_minutes": 15,
                "moderation_allow_everyone": False,
                "moderation_role_ids": [],
                "support_allow_everyone": False,
                "support_role_ids": [10],
                "community_allow_everyone": False,
                "community_role_ids": [11],
                "autorole_role_ids": [11],
                "welcome_channel_id": 3001,
                "welcome_message": "Welcome {user}",
                "leave_channel_id": 3002,
                "leave_message": "Bye {user_name}",
                "join_dm_enabled": True,
                "join_dm_message": "Read the rules",
                "support_issue_types": ["Moderation Help", "Other"],
                "support_command_channel_id": 3002,
            },
            headers={"origin": "http://testserver"},
        )

        self.assertEqual(response.status_code, 200)
        moderation_policy = self.bot.command_controls.get_policy(self.bot.guild.id, "warn")
        support_policy = self.bot.command_controls.get_policy(self.bot.guild.id, "ticketclaim")
        self.assertTrue(moderation_policy["restrict_to_roles"])
        self.assertEqual(moderation_policy["allowed_role_ids"], [])
        self.assertTrue(support_policy["restrict_to_roles"])
        self.assertEqual(support_policy["allowed_role_ids"], [10])
        self.assertEqual(self.bot.command_controls.get_autorole_role_ids(self.bot.guild.id), [11])
        self.assertEqual(self.bot.ticket_store.get_support_command_channel_id(self.bot.guild.id), 3002)
        self.assertTrue(self.bot.command_controls.is_setup_wizard_completed(self.bot.guild.id))

    def test_logout_clears_dashboard_session(self) -> None:
        self._authenticate()
        logout = self.client.get("/logout", follow_redirects=False)
        self.assertEqual(logout.status_code, 302)

        servers = self.client.get("/servers", follow_redirects=False)
        self.assertEqual(servers.status_code, 302)
        self.assertEqual(servers.headers["location"], "/")


if __name__ == "__main__":
    unittest.main()
