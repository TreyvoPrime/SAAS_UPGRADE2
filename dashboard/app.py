from __future__ import annotations

import asyncio
import os
import threading
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode

import httpx
import uvicorn
import discord
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from core.command_catalog import FREE_TIER, PREMIUM_TIER, build_command_catalog

MANAGE_GUILD = 0x20
ADMINISTRATOR = 0x08
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


class CommandPolicyPayload(BaseModel):
    command_name: str = Field(min_length=1)
    enabled: bool | None = None
    allowed_role_ids: list[int] | None = None


class DashboardAccessPayload(BaseModel):
    editor_role_ids: list[int] = Field(default_factory=list)


class DefenseTogglePayload(BaseModel):
    defense_name: str = Field(min_length=1)
    enabled: bool
    duration_minutes: int | None = Field(default=None, ge=1, le=10080)


class DefenseLockdownRolesPayload(BaseModel):
    lockdown_role_ids: list[int] = Field(default_factory=list)


class AutoFilterTermsPayload(BaseModel):
    terms: list[str] = Field(default_factory=list, max_length=100)


class GreetingConfigPayload(BaseModel):
    flow: str = Field(min_length=1)
    channel_id: int | str | None = None
    message: str | None = Field(default=None, max_length=1500)


class SupportSettingsPayload(BaseModel):
    issue_types: list[str] = Field(default_factory=list, max_length=20)


class PurgeSettingsPayload(BaseModel):
    limit: int = Field(ge=1, le=2000)


class ModerationSettingsPayload(BaseModel):
    confirmation_enabled: bool
    default_timeout_minutes: int = Field(ge=1, le=40320)


def resolve_dashboard_host() -> str:
    return os.getenv("DASHBOARD_HOST") or os.getenv("HOST") or "0.0.0.0"


def resolve_dashboard_port() -> int:
    return int(os.getenv("PORT") or os.getenv("DASHBOARD_PORT") or "8000")


def resolve_dashboard_base_url(host: str, port: int) -> str:
    explicit = os.getenv("DASHBOARD_BASE_URL")
    if explicit:
        return explicit.rstrip("/")

    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
    if railway_domain:
        return f"https://{railway_domain}".rstrip("/")

    return f"http://{host}:{port}".rstrip("/")


def create_dashboard_app(bot) -> FastAPI:
    app = FastAPI(title="ServerCore Dashboard")
    app.add_middleware(
        SessionMiddleware,
        secret_key=os.getenv("DASHBOARD_SECRET_KEY", "servercore-dashboard-secret"),
        same_site="lax",
        https_only=False,
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    def render_template(name: str, request: Request, context: dict) -> HTMLResponse:
        merged_context = {"request": request, **context}
        template = templates.get_template(name)
        return HTMLResponse(template.render(merged_context))

    dashboard_host = resolve_dashboard_host()
    dashboard_port = resolve_dashboard_port()
    discord_client_id = os.getenv("DISCORD_CLIENT_ID") or os.getenv("DISCORD_APP_ID")
    discord_client_secret = os.getenv("DISCORD_CLIENT_SECRET")
    dashboard_base_url = resolve_dashboard_base_url(dashboard_host, dashboard_port)
    redirect_uri = os.getenv("DISCORD_REDIRECT_URI") or f"{dashboard_base_url}/auth/callback"
    install_permissions = os.getenv("DISCORD_INSTALL_PERMISSIONS", "8")

    def oauth_ready() -> bool:
        return bool(discord_client_id and discord_client_secret)

    def session_user(request: Request) -> dict | None:
        return request.session.get("discord_user")

    def _guild_icon_url(raw_guild: dict, live_guild) -> str:
        if live_guild and getattr(live_guild, "icon", None):
            return live_guild.icon.url

        icon_hash = raw_guild.get("icon")
        if icon_hash:
            return f"https://cdn.discordapp.com/icons/{raw_guild['id']}/{icon_hash}.png?size=256"

        guild_id = int(raw_guild["id"])
        return f"https://cdn.discordapp.com/embed/avatars/{guild_id % 5}.png"

    def build_install_url(guild_id: int) -> str:
        params = urlencode(
            {
                "client_id": discord_client_id,
                "permissions": install_permissions,
                "guild_id": guild_id,
                "disable_guild_select": "true",
                "integration_type": "0",
                "scope": "bot applications.commands",
            }
        )
        return f"https://discord.com/oauth2/authorize?{params}"

    async def fetch_discord_token(code: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://discord.com/api/oauth2/token",
                data={
                    "client_id": discord_client_id,
                    "client_secret": discord_client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            return response.json()

    async def fetch_discord_resource(access_token: str, resource: str) -> dict | list:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"https://discord.com/api{resource}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 5))
                await asyncio.sleep(retry_after)
                response = await client.get(
                    f"https://discord.com/api{resource}",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            response.raise_for_status()
            return response.json()

    async def fetch_user_guild_member_roles(user_id: int, guild_id: int) -> set[int]:
        guild = bot.get_guild(guild_id)
        if guild is None:
            return set()

        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                return set()

        return {role.id for role in getattr(member, "roles", [])}

    async def run_on_bot_loop(coro):
        bot_loop = getattr(bot, "runtime_loop", None) or getattr(bot, "loop", None)
        current_loop = asyncio.get_running_loop()
        if bot_loop is None:
            raise HTTPException(status_code=503, detail="Bot event loop is not available")
        if getattr(bot_loop, "is_closed", lambda: False)():
            raise HTTPException(status_code=503, detail="Bot event loop is closed")
        if bot_loop == current_loop:
            return await coro

        future = asyncio.run_coroutine_threadsafe(coro, bot_loop)
        return await asyncio.wrap_future(future)

    async def log_dashboard_event(
        request: Request,
        guild_id: int,
        *,
        title: str,
        description: str,
        fields: list[tuple[str, str, bool]] | None = None,
    ) -> None:
        audit_cog = bot.get_cog("AuditLogCog")
        user = session_user(request) or {}
        user_name = user.get("global_name") or user.get("username") or "Dashboard User"
        if audit_cog is not None and hasattr(audit_cog, "emit_external_event"):
            await run_on_bot_loop(
                audit_cog.emit_external_event(
                    guild_id,
                    title=title,
                    description=description,
                    status="event",
                    color=discord.Color.blurple(),
                    user_name=user_name,
                    channel_name="Dashboard",
                    fields=fields,
                )
            )
        elif hasattr(bot, "command_logs"):
            bot.command_logs.append(
                {
                    "guild_id": guild_id,
                    "guild_name": bot.get_guild(guild_id).name if bot.get_guild(guild_id) else "Unknown Guild",
                    "kind": "event",
                    "title": title,
                    "summary": description,
                    "status": "event",
                    "user_name": user_name,
                    "channel_name": "Dashboard",
                }
            )

    def premium_enabled() -> bool:
        return any(command["tier"] == PREMIUM_TIER for command in build_command_catalog(bot))

    async def build_live_guild_entry(guild_id: int, user_id: int) -> dict | None:
        guild = bot.get_guild(guild_id)
        if guild is None:
            return None

        owner = guild.owner_id == user_id
        member = None
        user_role_ids: set[int] = set()
        if not owner:
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except Exception:
                    return None

            user_role_ids = {role.id for role in getattr(member, "roles", [])}

        can_manage_guild = owner or bool(
            member
            and (
                member.guild_permissions.manage_guild
                or member.guild_permissions.administrator
            )
        )
        editor_role_ids = set(bot.access_manager.controls.get_dashboard_editor_roles(guild_id))
        has_editor_role = bool(editor_role_ids & user_role_ids)
        can_edit_dashboard = can_manage_guild or has_editor_role
        if not can_edit_dashboard:
            return None

        return {
            "id": guild.id,
            "name": guild.name,
            "icon_url": _guild_icon_url({"id": str(guild.id), "icon": getattr(guild.icon, "key", None)}, guild),
            "initials": guild.name[:2].upper(),
            "member_count": guild.member_count or len(guild.members),
            "role_count": max(len(guild.roles) - 1, 0),
            "premium_enabled": premium_enabled(),
            "bot_installed": True,
            "dashboard_url": f"/dashboard/{guild.id}",
            "install_url": build_install_url(guild.id),
            "owner": owner,
            "can_manage_guild": can_manage_guild,
            "can_edit_dashboard": can_edit_dashboard,
            "can_manage_editor_roles": can_manage_guild,
        }

    async def load_user_guilds(request: Request) -> list[dict]:
        access_token = request.session.get("access_token")
        user = session_user(request)
        if not access_token:
            return []
        if not user or not str(user.get("id", "")).isdigit():
            return []

        try:
            discord_guilds = await fetch_discord_resource(access_token, "/users/@me/guilds")
        except Exception:
            return []

        premium_available = premium_enabled()
        user_id = int(user["id"])
        manageable_guilds: list[dict] = []
        for guild in discord_guilds:
            permissions = int(guild.get("permissions", 0))
            guild_id = int(guild["id"])
            owner = bool(guild.get("owner"))
            can_manage_guild = owner or bool(permissions & MANAGE_GUILD or permissions & ADMINISTRATOR)
            editor_role_ids = set(bot.access_manager.controls.get_dashboard_editor_roles(guild_id))
            has_editor_role = False
            if editor_role_ids and not can_manage_guild:
                has_editor_role = bool(editor_role_ids & await fetch_user_guild_member_roles(user_id, guild_id))

            can_edit_dashboard = can_manage_guild or has_editor_role
            if not can_edit_dashboard:
                continue

            bot_guild = bot.get_guild(guild_id)
            manageable_guilds.append(
                {
                    "id": guild_id,
                    "name": guild["name"],
                    "icon_url": _guild_icon_url(guild, bot_guild),
                    "initials": guild["name"][:2].upper(),
                    "member_count": (bot_guild.member_count or len(bot_guild.members)) if bot_guild else None,
                    "role_count": max(len(bot_guild.roles) - 1, 0) if bot_guild else None,
                    "premium_enabled": premium_available,
                    "bot_installed": bot_guild is not None,
                    "dashboard_url": f"/dashboard/{guild_id}",
                    "install_url": build_install_url(guild_id),
                    "owner": owner,
                    "can_manage_guild": can_manage_guild,
                    "can_edit_dashboard": can_edit_dashboard,
                    "can_manage_editor_roles": can_manage_guild,
                }
            )

        manageable_guilds.sort(key=lambda item: item["name"].lower())
        return manageable_guilds

    async def require_user(request: Request) -> dict:
        user = session_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Login required")
        return user

    async def require_guild_access(
        request: Request,
        guild_id: int,
        *,
        require_editor_role_management: bool = False,
        require_bot_installed: bool = True,
    ) -> tuple[dict, list[dict]]:
        user = await require_user(request)
        guilds = await load_user_guilds(request)
        selected = next((guild for guild in guilds if guild["id"] == guild_id), None)
        if selected is None and str(user.get("id", "")).isdigit():
            selected = await build_live_guild_entry(guild_id, int(user["id"]))
            if selected is not None:
                guilds = [selected, *[guild for guild in guilds if guild["id"] != guild_id]]
        if selected is None:
            raise HTTPException(status_code=403, detail="Guild access denied")
        if require_bot_installed and not selected.get("bot_installed"):
            raise HTTPException(status_code=409, detail="Bot not installed in guild")
        if require_editor_role_management and not selected.get("can_manage_editor_roles"):
            raise HTTPException(status_code=403, detail="Only the owner or members with Manage Server can edit dashboard roles")
        return selected, guilds

    def guild_roles(guild_id: int) -> list[dict]:
        guild = bot.get_guild(guild_id)
        if guild is None:
            return []

        roles = [
            {
                "id": role.id,
                "name": role.name,
                "position": role.position,
                "label": role.name.lower(),
            }
            for role in guild.roles
            if role.name != "@everyone"
        ]
        roles.sort(key=lambda role: role["position"], reverse=True)
        return roles

    def guild_text_channels(guild_id: int) -> list[dict]:
        guild = bot.get_guild(guild_id)
        if guild is None:
            return []

        channels = [
            {
                "id": channel.id,
                "name": channel.name,
                "label": f"#{channel.name}",
                "position": channel.position,
            }
            for channel in guild.text_channels
        ]
        channels.sort(key=lambda item: item["position"])
        return channels

    def dashboard_access_summary(guild_id: int) -> dict:
        role_lookup = {role["id"]: role["name"] for role in guild_roles(guild_id)}
        editor_role_ids = bot.access_manager.controls.get_dashboard_editor_roles(guild_id)
        return {
            "editor_role_ids": editor_role_ids,
            "editor_role_names": [role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in editor_role_ids],
        }

    def purge_settings_summary(guild_id: int) -> dict:
        controls = bot.access_manager.controls
        current_limit = controls.get_purge_limit(guild_id)
        max_limit = controls.FREE_PURGE_LIMIT_CAP
        premium_limit = controls.PREMIUM_PURGE_LIMIT_CAP
        return {
            "limit": min(current_limit, max_limit),
            "max_limit": max_limit,
            "premium_limit": premium_limit,
        }

    def moderation_settings_summary(guild_id: int) -> dict:
        controls = bot.access_manager.controls
        settings = controls.get_moderation_settings(guild_id)
        return {
            "confirmation_enabled": settings["confirmation_enabled"],
            "default_timeout_minutes": settings["default_timeout_minutes"],
        }

    def greetings_dashboard_summary(guild_id: int) -> dict:
        manager = getattr(bot, "greetings", None)
        channels = guild_text_channels(guild_id)
        channel_lookup = {channel["id"]: channel["label"] for channel in channels}
        if manager is not None and hasattr(manager, "get_dashboard_state"):
            return manager.get_dashboard_state(guild_id, channel_lookup)

        return {
            "welcome": {
                "channel_id": None,
                "channel_name": "Not configured",
                "message": "Hello {user}, welcome to {server}.",
                "enabled": False,
            },
            "leave": {
                "channel_id": None,
                "channel_name": "Not configured",
                "message": "{user_name} left {server}.",
                "enabled": False,
            },
            "join_dm": {
                "enabled": False,
                "message": "Welcome to {server}, {display_name}. Read the server guide and check the rules channel to get started.",
            },
            "placeholders": [
                {"token": "{user}", "label": "Mentions the member"},
                {"token": "{user_name}", "label": "Uses the member name"},
                {"token": "{display_name}", "label": "Uses the server nickname"},
                {"token": "{server}", "label": "Uses the server name"},
                {"token": "{membercount}", "label": "Uses the current member count"},
            ],
        }

    def support_dashboard_summary(guild_id: int) -> dict:
        ticket_store = getattr(bot, "ticket_store", None)
        if ticket_store is None:
            return {
                "issue_types": [],
                "issue_count": 0,
                "support_category_id": None,
                "support_category_name": "Not configured yet",
            }

        category_id = ticket_store.get_support_category_id(guild_id)
        guild = bot.get_guild(guild_id)
        category_name = "Not configured yet"
        if guild is not None and category_id:
            category = guild.get_channel(category_id)
            if category is not None:
                category_name = getattr(category, "name", "Configured")
        return {
            "issue_types": ticket_store.get_issue_types(guild_id),
            "issue_count": len(ticket_store.get_issue_types(guild_id)),
            "support_category_id": category_id,
            "support_category_name": category_name,
            "active_tickets": ticket_store.list_tickets(guild_id, status="open", limit=10),
        }

    def giveaways_dashboard_summary(guild_id: int) -> dict:
        store = getattr(bot, "giveaway_store", None)
        if store is None:
            return {"active": [], "recent": [], "active_count": 0, "recent_count": 0}

        def serialize(item: dict) -> dict:
            return {
                "id": item["id"],
                "prize": item.get("prize", "Giveaway"),
                "winner_count": int(item.get("winner_count", 1)),
                "entry_count": len(item.get("entrants", [])),
                "status": item.get("status", "active"),
                "host_name": item.get("host_name", "Unknown"),
                "ends_at": item.get("ends_at"),
                "ended_at": item.get("ended_at"),
                "winner_names": item.get("winner_names", []),
                "description": item.get("description") or "No extra details set.",
                "required_role_ids": item.get("required_role_ids", []),
                "bonus_role_ids": item.get("bonus_role_ids", []),
                "bonus_entries": int(item.get("bonus_entries", 0)),
            }

        active = [serialize(item) for item in store.list_giveaways(guild_id, status="active", limit=12)]
        recent = [serialize(item) for item in store.list_giveaways(guild_id, status="ended", limit=8)]
        return {
            "active": active,
            "recent": recent,
            "active_count": len(active),
            "recent_count": len(recent),
        }

    def autofeed_dashboard_summary(guild_id: int) -> dict:
        store = getattr(bot, "autofeed_store", None)
        if store is None:
            return {"feeds": [], "count": 0}

        feeds = []
        for item in store.list_feeds(guild_id):
            feeds.append(
                {
                    "id": item["id"],
                    "channel_id": item["channel_id"],
                    "message": item["message"],
                    "interval_minutes": item["interval_minutes"],
                    "next_post_at": item.get("next_post_at"),
                    "created_by_name": item.get("created_by_name", "Unknown"),
                    "enabled": bool(item.get("enabled", True)),
                }
            )
        return {"feeds": feeds, "count": len(feeds)}

    def server_defense_summary(guild_id: int) -> dict:
        defense = bot.server_defense.get_dashboard_state(guild_id)
        role_lookup = {role["id"]: role["name"] for role in guild_roles(guild_id)}
        lockdown = defense.get("lockdown", {})
        lockdown_role_ids = lockdown.get("allowed_role_ids", [])
        return {
            **defense,
            "lockdown_allowed_role_names": [
                role_lookup.get(role_id, f"Deleted ({role_id})")
                for role_id in lockdown_role_ids
            ],
        }

    def defense_dashboard_summary(guild_id: int) -> dict:
        role_lookup = {role["id"]: role["name"] for role in guild_roles(guild_id)}
        manager = getattr(bot, "server_defense", None)
        if manager is not None and hasattr(manager, "build_dashboard_state"):
            return manager.build_dashboard_state(guild_id, role_lookup)

        return {
            "cards": [
                {
                    "name": "linkblock",
                    "title": "Link Block",
                    "tag": "Inbound links",
                    "description": "Blocks external URLs before they land in chat.",
                    "enabled": False,
                    "duration_minutes": None,
                    "duration_label": "Until disabled",
                    "status_label": "Offline",
                    "remaining_label": "No timer",
                    "tone": "muted",
                    "rate_label": None,
                },
                {
                    "name": "inviteblock",
                    "title": "Invite Block",
                    "tag": "Discord invites",
                    "description": "Blocks Discord invite links separately from normal URLs.",
                    "enabled": False,
                    "duration_minutes": None,
                    "duration_label": "Until disabled",
                    "status_label": "Offline",
                    "remaining_label": "No timer",
                    "tone": "muted",
                    "rate_label": None,
                },
                {
                    "name": "antispam",
                    "title": "Anti Spam",
                    "tag": "Message rate",
                    "description": "Catches rapid message bursts early, clears the burst, and cools the user down with a timeout.",
                    "enabled": False,
                    "duration_minutes": None,
                    "duration_label": "Until disabled",
                    "status_label": "Offline",
                    "remaining_label": "No timer",
                    "tone": "muted",
                    "rate_label": "5 messages / 6 seconds",
                },
                {
                    "name": "antijoin",
                    "title": "Anti Join",
                    "tag": "Join control",
                    "description": "Kicks new joins while active so raids cannot build momentum.",
                    "enabled": False,
                    "duration_minutes": None,
                    "duration_label": "Until disabled",
                    "status_label": "Offline",
                    "remaining_label": "No timer",
                    "tone": "muted",
                    "rate_label": None,
                },
                {
                    "name": "mentionguard",
                    "title": "Mention Guard",
                    "tag": "Ping shield",
                    "description": "Tracks mention bursts across messages so staff can stop ping raids before they spread.",
                    "enabled": False,
                    "duration_minutes": None,
                    "duration_label": "Until disabled",
                    "status_label": "Offline",
                    "remaining_label": "No timer",
                    "tone": "muted",
                    "rate_label": "5 mentions / 10 seconds",
                },
                {
                    "name": "autofilter",
                    "title": "AutoFilter",
                    "tag": "Blocked words",
                    "description": "Blocks flagged words or phrases, warns members up to three times, then times them out for one hour if they keep pushing it.",
                    "enabled": False,
                    "duration_minutes": None,
                    "duration_label": "Until disabled",
                    "status_label": "Offline",
                    "remaining_label": "No timer",
                    "tone": "muted",
                    "rate_label": "3 warnings, then 60-minute timeout",
                },
                {
                    "name": "lockdown",
                    "title": "Lockdown",
                    "tag": "Channel freeze",
                    "description": "Locks text channels down and keeps selected talk roles moving while the server is under pressure.",
                    "enabled": False,
                    "duration_minutes": None,
                    "duration_label": "Until disabled",
                    "status_label": "Offline",
                    "remaining_label": "No timer",
                    "tone": "muted",
                    "rate_label": None,
                    "allowed_role_ids": [],
                    "allowed_role_names": [],
                    "allowed_role_summary": "Only server staff can talk",
                },
                {
                    "name": "antiraid",
                    "title": "Guardian",
                    "tag": "Threat scoring",
                    "description": "Scores suspicious bursts, fresh-account joins, repeated content, and stacked defenses before a raid fully lands.",
                    "enabled": False,
                    "duration_minutes": None,
                    "duration_label": "Until disabled",
                    "status_label": "Offline",
                    "remaining_label": "No timer",
                    "tone": "muted",
                    "rate_label": "Live score + automatic response ladder",
                },
            ],
            "active_count": 0,
            "timed_count": 0,
            "lockdown_role_ids": [],
            "lockdown_role_names": [],
            "lockdown_role_count": 0,
            "autofilter_terms": [],
            "autofilter_warning_limit": 3,
            "autofilter_timeout_minutes": 60,
            "threat": {
                "enabled": False,
                "score": 0,
                "score_display": "0/100",
                "level_key": "normal",
                "level_label": "Offline",
                "progress_percent": 0,
                "status_copy": "Turn Guardian on to start watching for coordinated joins, spam bursts, and stacked guard triggers.",
                "recent_signals": [],
                "recent_actions": [],
                "raid_mode_active": False,
                "next_threshold": 25,
                "bands": [],
            },
        }

    def case_dashboard_summary(guild_id: int) -> dict:
        case_store = getattr(bot, "case_store", None)
        note_store = getattr(bot, "staff_note_store", None)
        if case_store is None:
            return {"cases": [], "open_count": 0, "recent_staff_notes": []}

        cases = case_store.list_cases(guild_id, 30)
        serialized = []
        for case in cases:
            notes = case.get("notes", [])
            serialized.append(
                {
                    "case_id": case["case_id"],
                    "action": case["action"],
                    "target_user_name": case["target_user_name"],
                    "moderator_name": case["moderator_name"],
                    "reason": case["reason"],
                    "created_at": case["created_at"],
                    "duration_minutes": case.get("duration_minutes"),
                    "note_count": len(notes),
                    "notes": notes[-5:],
                }
            )
        recent_staff_notes: list[dict] = []
        if note_store is not None:
            raw = note_store.data.get("guilds", {}).get(str(guild_id), {}).get("users", {})
            for user_id, notes in raw.items():
                for note in notes[-3:]:
                    recent_staff_notes.append(
                        {
                            "user_id": user_id,
                            "note_id": note.get("note_id"),
                            "moderator_name": note.get("moderator_name", "Unknown"),
                            "note": note.get("note", ""),
                            "timestamp": note.get("timestamp", ""),
                        }
                    )
            recent_staff_notes.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
        return {"cases": serialized, "open_count": len(serialized), "recent_staff_notes": recent_staff_notes[:8]}

    def _slugify(value: str) -> str:
        return "".join(character.lower() if character.isalnum() else "-" for character in value).strip("-")

    def guild_command_rows(guild_id: int) -> tuple[list[dict], list[dict]]:
        roles = guild_roles(guild_id)
        role_names = {role["id"]: role["name"] for role in roles}
        grouped: dict[str, list[dict]] = defaultdict(list)
        rows: list[dict] = []

        for command in build_command_catalog(bot):
            policy = bot.access_manager.controls.get_policy(guild_id, command["name"])
            allowed_role_ids = policy["allowed_role_ids"] or []
            row = {
                **command,
                "slug": _slugify(command["name"]),
                "module_slug": _slugify(command["module"]),
                "tier_slug": command["tier"].lower(),
                "enabled": policy["enabled"],
                "allowed_role_ids": allowed_role_ids,
                "allowed_role_names": [role_names.get(role_id, f"Deleted ({role_id})") for role_id in allowed_role_ids],
                "allowed_role_count": len(allowed_role_ids),
                "status_label": "Enabled" if policy["enabled"] else "Disabled",
            }
            rows.append(row)
            grouped[row["module"]].append(row)

        module_cards = []
        for module_name, commands in sorted(grouped.items(), key=lambda item: item[0].lower()):
            module_cards.append(
                {
                    "name": module_name,
                    "slug": _slugify(module_name),
                    "count": len(commands),
                    "disabled_count": len([command for command in commands if not command["enabled"]]),
                    "restricted_count": len([command for command in commands if command["allowed_role_ids"]]),
                    "tier": PREMIUM_TIER if any(command["tier"] == PREMIUM_TIER for command in commands) else FREE_TIER,
                    "commands": sorted(commands, key=lambda item: item["name"]),
                }
            )

        rows.sort(key=lambda item: (item["module"].lower(), item["name"]))
        return rows, module_cards

    async def render_dashboard_view(request: Request, guild_id: int, current_view: str) -> HTMLResponse | RedirectResponse:
        if session_user(request) is None:
            return RedirectResponse(url="/", status_code=302)
        selected_guild, guilds = await require_guild_access(request, guild_id, require_bot_installed=False)
        if not selected_guild.get("bot_installed"):
            return RedirectResponse(url=selected_guild["install_url"], status_code=302)
        roles = guild_roles(guild_id)
        text_channels = guild_text_channels(guild_id)
        commands, module_cards = guild_command_rows(guild_id)
        logs = bot.command_logs.list_for_guild(guild_id, 80) if hasattr(bot, "command_logs") else []
        access_summary = dashboard_access_summary(guild_id)
        defense_summary = defense_dashboard_summary(guild_id)
        greetings_summary = greetings_dashboard_summary(guild_id)
        support_summary = support_dashboard_summary(guild_id)
        giveaways_summary = giveaways_dashboard_summary(guild_id)
        autofeed_summary = autofeed_dashboard_summary(guild_id)
        case_summary = case_dashboard_summary(guild_id)
        purge_settings = purge_settings_summary(guild_id)
        moderation_settings = moderation_settings_summary(guild_id)

        stats = {
            "commands": len(commands),
            "disabled": len([command for command in commands if not command["enabled"]]),
            "restricted": len([command for command in commands if command["allowed_role_ids"]]),
            "roles": len(roles),
            "modules": len(module_cards),
        }

        return render_template(
            "dashboard.html",
            request,
            {
                "bot_name": bot.user.name if getattr(bot, "user", None) else "ServerCore",
                "user": session_user(request),
                "guilds": guilds,
                "selected_guild": selected_guild,
                "commands": commands,
                "sections": module_cards,
                "modules": module_cards,
                "roles": roles,
                "text_channels": text_channels,
                "logs": logs,
                "stats": stats,
                "dashboard_base_url": dashboard_base_url,
                "dashboard_editor_role_ids": access_summary["editor_role_ids"],
                "dashboard_editor_role_names": access_summary["editor_role_names"],
                "can_manage_editor_roles": selected_guild["can_manage_editor_roles"],
                "defense_cards": defense_summary["cards"],
                "defense_summary": defense_summary,
                "can_manage_lockdown_roles": selected_guild["can_manage_editor_roles"],
                "greetings_summary": greetings_summary,
                "support_summary": support_summary,
                "giveaways_summary": giveaways_summary,
                "autofeed_summary": autofeed_summary,
                "case_summary": case_summary,
                "purge_settings": purge_settings,
                "moderation_settings": moderation_settings,
                "current_view": current_view,
            },
        )

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "guilds": len(bot.guilds) if bot else 0}

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        user = session_user(request)
        if user:
            return RedirectResponse(url="/servers", status_code=302)

        return render_template(
            "login.html",
            request,
            {
                "oauth_ready": oauth_ready(),
                "bot_name": bot.user.name if getattr(bot, "user", None) else "ServerCore",
                "dashboard_base_url": dashboard_base_url,
            },
        )

    @app.get("/servers", response_class=HTMLResponse)
    async def servers(request: Request):
        user = session_user(request)
        if user is None:
            return RedirectResponse(url="/", status_code=302)
        guilds = await load_user_guilds(request)
        premium_count = len([command for command in build_command_catalog(bot) if command["tier"] == PREMIUM_TIER])
        return render_template(
            "guilds.html",
            request,
            {
                "bot_name": bot.user.name if getattr(bot, "user", None) else "ServerCore",
                "user": user,
                "guilds": guilds,
                "premium_count": premium_count,
            },
        )

    @app.get("/login")
    async def login():
        if not oauth_ready():
            raise HTTPException(status_code=500, detail="Discord OAuth is not configured")

        query = urlencode(
            {
                "client_id": discord_client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "identify guilds",
                "prompt": "consent",
            }
        )
        return RedirectResponse(url=f"https://discord.com/oauth2/authorize?{query}", status_code=302)

    @app.get("/auth/callback")
    async def auth_callback(request: Request, code: str):
        token_data = await fetch_discord_token(code)
        user_data = await fetch_discord_resource(token_data["access_token"], "/users/@me")

        request.session["access_token"] = token_data["access_token"]
        request.session["discord_user"] = {
            "id": user_data["id"],
            "username": user_data["username"],
            "global_name": user_data.get("global_name"),
            "avatar": user_data.get("avatar"),
        }
        return RedirectResponse(url="/servers", status_code=302)

    @app.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse(url="/", status_code=302)

    @app.get("/dashboard/{guild_id}", response_class=HTMLResponse)
    async def dashboard(request: Request, guild_id: int):
        return await render_dashboard_view(request, guild_id, "commands")

    @app.get("/dashboard/{guild_id}/defense", response_class=HTMLResponse)
    async def defense_dashboard(request: Request, guild_id: int):
        return await render_dashboard_view(request, guild_id, "defense")

    @app.get("/dashboard/{guild_id}/greetings", response_class=HTMLResponse)
    async def greetings_dashboard(request: Request, guild_id: int):
        return await render_dashboard_view(request, guild_id, "greetings")

    @app.get("/dashboard/{guild_id}/support", response_class=HTMLResponse)
    async def support_dashboard(request: Request, guild_id: int):
        return await render_dashboard_view(request, guild_id, "support")

    @app.get("/dashboard/{guild_id}/giveaways", response_class=HTMLResponse)
    async def giveaways_dashboard(request: Request, guild_id: int):
        return await render_dashboard_view(request, guild_id, "giveaways")

    @app.get("/dashboard/{guild_id}/autofeed", response_class=HTMLResponse)
    async def autofeed_dashboard(request: Request, guild_id: int):
        return await render_dashboard_view(request, guild_id, "autofeed")

    @app.get("/api/guilds/{guild_id}/logs")
    async def guild_logs(request: Request, guild_id: int, limit: int = 80):
        await require_guild_access(request, guild_id)
        logs = bot.command_logs.list_for_guild(guild_id, min(limit, 150)) if hasattr(bot, "command_logs") else []
        return JSONResponse({"entries": logs})

    @app.post("/api/guilds/{guild_id}/command-policy")
    async def update_command_policy(request: Request, guild_id: int, payload: CommandPolicyPayload):
        await require_guild_access(request, guild_id)

        commands = build_command_catalog(bot)
        if not any(command["name"] == payload.command_name for command in commands):
            raise HTTPException(status_code=404, detail="Unknown command")

        valid_role_ids = {role["id"] for role in guild_roles(guild_id)}
        policy = bot.access_manager.controls.get_policy(guild_id, payload.command_name)

        if payload.enabled is not None:
            policy = bot.access_manager.controls.set_enabled(guild_id, payload.command_name, payload.enabled)

        if payload.allowed_role_ids is not None:
            safe_role_ids = [role_id for role_id in payload.allowed_role_ids if role_id in valid_role_ids]
            policy = bot.access_manager.controls.set_roles(guild_id, payload.command_name, safe_role_ids)

        role_lookup = {role["id"]: role["name"] for role in guild_roles(guild_id)}
        await log_dashboard_event(
            request,
            guild_id,
            title="Dashboard Command Policy Updated",
            description=f"Updated access for /{payload.command_name} from the dashboard.",
            fields=[
                ("Enabled", "Yes" if policy["enabled"] else "No", True),
                ("Allowed Roles", ", ".join(role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in policy["allowed_role_ids"]) or "Discord native checks only", False),
            ],
        )
        return JSONResponse(
            {
                "command_name": payload.command_name,
                "policy": policy,
                "allowed_role_names": [role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in policy["allowed_role_ids"]],
            }
        )

    @app.get("/api/guilds/{guild_id}/defense-state")
    async def get_defense_state(request: Request, guild_id: int):
        await require_guild_access(request, guild_id)
        return JSONResponse(defense_dashboard_summary(guild_id))

    @app.post("/api/guilds/{guild_id}/defense-state")
    async def update_defense_state(request: Request, guild_id: int, payload: DefenseTogglePayload):
        await require_guild_access(request, guild_id)
        manager = getattr(bot, "server_defense", None)
        if manager is None or not hasattr(manager, "set_defense"):
            raise HTTPException(status_code=503, detail="ServerDefense is not available")

        if payload.defense_name not in {"linkblock", "inviteblock", "antispam", "antijoin", "mentionguard", "autofilter", "lockdown", "antiraid"}:
            raise HTTPException(status_code=404, detail="Unknown defense")

        result = await run_on_bot_loop(
            manager.set_defense(
                guild_id,
                payload.defense_name,
                enabled=payload.enabled,
                duration_minutes=payload.duration_minutes,
            )
        )
        role_lookup = {role["id"]: role["name"] for role in guild_roles(guild_id)}
        state = manager.build_dashboard_state(guild_id, role_lookup)
        card = next((item for item in state["cards"] if item["name"] == payload.defense_name), result)
        await log_dashboard_event(
            request,
            guild_id,
            title="Dashboard ServerGuard Updated",
            description=f"{card['title']} was {'enabled' if card['enabled'] else 'disabled'} from the dashboard.",
            fields=[("Duration", card.get("duration_label") or "Until disabled", False)],
        )
        return JSONResponse({"card": card, "state": state})

    @app.post("/api/guilds/{guild_id}/autofilter-terms")
    async def update_autofilter_terms(request: Request, guild_id: int, payload: AutoFilterTermsPayload):
        await require_guild_access(request, guild_id)
        manager = getattr(bot, "server_defense", None)
        if manager is None or not hasattr(manager, "update_autofilter_terms"):
            raise HTTPException(status_code=503, detail="ServerDefense is not available")

        cleaned_terms = []
        seen: set[str] = set()
        for term in payload.terms:
            normalized = str(term).strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            cleaned_terms.append(normalized)

        updated = await run_on_bot_loop(manager.update_autofilter_terms(guild_id, cleaned_terms))
        state = await run_on_bot_loop(
            asyncio.to_thread(
                manager.build_dashboard_state,
                guild_id,
                {role["id"]: role["name"] for role in guild_roles(guild_id)},
            )
        )
        await log_dashboard_event(
            request,
            guild_id,
            title="Dashboard AutoFilter Updated",
            description="Updated the blocked words and phrases in AutoFilter.",
            fields=[("Term Count", str(len(updated.get("filter_terms", []))), True)],
        )
        return JSONResponse(
            {
                "terms": updated.get("filter_terms", []),
                "warning_limit": updated.get("warning_limit", 3),
                "timeout_minutes": updated.get("timeout_minutes", 60),
                "state": state,
            }
        )

    @app.post("/api/guilds/{guild_id}/defense-lockdown-roles")
    async def update_defense_lockdown_roles(request: Request, guild_id: int, payload: DefenseLockdownRolesPayload):
        await require_guild_access(request, guild_id, require_editor_role_management=True)
        manager = getattr(bot, "server_defense", None)
        if manager is None or not (hasattr(manager, "ensure_lockdown_roles") or hasattr(manager, "set_lockdown_roles")):
            raise HTTPException(status_code=503, detail="ServerDefense is not available")

        valid_role_ids = {role["id"] for role in guild_roles(guild_id)}
        safe_role_ids = [role_id for role_id in payload.lockdown_role_ids if role_id in valid_role_ids]
        if hasattr(manager, "ensure_lockdown_roles"):
            role_ids = await run_on_bot_loop(manager.ensure_lockdown_roles(guild_id, safe_role_ids))
        else:
            updated = await run_on_bot_loop(manager.set_lockdown_roles(guild_id, safe_role_ids))
            role_ids = list(updated.get("allowed_role_ids", []))
        role_lookup = {role["id"]: role["name"] for role in guild_roles(guild_id)}
        state = manager.build_dashboard_state(guild_id, role_lookup)
        await log_dashboard_event(
            request,
            guild_id,
            title="Dashboard Lockdown Roles Updated",
            description="Updated which roles can keep talking during lockdown.",
            fields=[
                ("Allowed Roles", ", ".join(role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in role_ids) or "Only server staff", False),
            ],
        )
        return JSONResponse(
            {
                "lockdown_role_ids": role_ids,
                "lockdown_role_names": [role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in role_ids],
                "state": state,
            }
        )

    @app.get("/api/guilds/{guild_id}/dashboard-access")
    async def get_dashboard_access(request: Request, guild_id: int):
        await require_guild_access(request, guild_id)
        return JSONResponse(dashboard_access_summary(guild_id))

    @app.post("/api/guilds/{guild_id}/dashboard-access")
    async def update_dashboard_access(request: Request, guild_id: int, payload: DashboardAccessPayload):
        await require_guild_access(request, guild_id, require_editor_role_management=True)
        valid_role_ids = {role["id"] for role in guild_roles(guild_id)}
        safe_role_ids = [role_id for role_id in payload.editor_role_ids if role_id in valid_role_ids]
        editor_role_ids = bot.access_manager.controls.set_dashboard_editor_roles(guild_id, safe_role_ids)
        role_lookup = {role["id"]: role["name"] for role in guild_roles(guild_id)}
        await log_dashboard_event(
            request,
            guild_id,
            title="Dashboard Editor Roles Updated",
            description="Updated who can manage this dashboard.",
            fields=[
                ("Editor Roles", ", ".join(role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in editor_role_ids) or "Owner and Manage Server only", False),
            ],
        )
        return JSONResponse(
            {
                "editor_role_ids": editor_role_ids,
                "editor_role_names": [role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in editor_role_ids],
            }
        )

    @app.get("/api/guilds/{guild_id}/greetings")
    async def get_greetings(request: Request, guild_id: int):
        await require_guild_access(request, guild_id)
        return JSONResponse(greetings_dashboard_summary(guild_id))

    @app.post("/api/guilds/{guild_id}/greetings")
    async def update_greetings(request: Request, guild_id: int, payload: GreetingConfigPayload):
        await require_guild_access(request, guild_id)

        manager = getattr(bot, "greetings", None)
        if manager is None:
            raise HTTPException(status_code=503, detail="Welcome / Leave is not available")

        flow = payload.flow.strip().lower()
        if flow not in {"welcome", "leave"}:
            raise HTTPException(status_code=404, detail="Unknown greeting flow")

        valid_channel_ids = {channel["id"] for channel in guild_text_channels(guild_id)}
        try:
            requested_channel_id = int(payload.channel_id) if payload.channel_id not in (None, "") else None
        except (TypeError, ValueError):
            requested_channel_id = None
        channel_id = requested_channel_id if requested_channel_id in valid_channel_ids else None
        message = (payload.message or "").strip() or None

        if flow == "welcome":
            manager.set_welcome(guild_id, channel_id=channel_id, message=message)
        else:
            manager.set_leave(guild_id, channel_id=channel_id, message=message)

        await log_dashboard_event(
            request,
            guild_id,
            title="Dashboard Greeting Updated",
            description=f"Updated the {flow} flow from the dashboard.",
            fields=[
                ("Channel", next((channel["label"] for channel in guild_text_channels(guild_id) if channel["id"] == channel_id), "Disabled"), False),
                ("Message", (message or "Default message")[:240], False),
            ],
        )
        return JSONResponse(greetings_dashboard_summary(guild_id))

    @app.get("/api/guilds/{guild_id}/support-settings")
    async def get_support_settings(request: Request, guild_id: int):
        await require_guild_access(request, guild_id)
        return JSONResponse(support_dashboard_summary(guild_id))

    @app.post("/api/guilds/{guild_id}/support-settings")
    async def update_support_settings(request: Request, guild_id: int, payload: SupportSettingsPayload):
        await require_guild_access(request, guild_id)

        ticket_store = getattr(bot, "ticket_store", None)
        if ticket_store is None:
            raise HTTPException(status_code=503, detail="Support settings are not available")

        cleaned_issue_types = [
            str(issue_type).strip()
            for issue_type in payload.issue_types
            if str(issue_type).strip()
        ]
        updated_issue_types = ticket_store.set_issue_types(guild_id, cleaned_issue_types)
        await log_dashboard_event(
            request,
            guild_id,
            title="Dashboard Support Intake Updated",
            description="Updated the support issue list used by /ticket.",
            fields=[("Issue Types", ", ".join(updated_issue_types), False)],
        )
        return JSONResponse(support_dashboard_summary(guild_id))

    @app.get("/api/guilds/{guild_id}/purge-settings")
    async def get_purge_settings(request: Request, guild_id: int):
        await require_guild_access(request, guild_id)
        return JSONResponse(purge_settings_summary(guild_id))

    @app.post("/api/guilds/{guild_id}/purge-settings")
    async def update_purge_settings(request: Request, guild_id: int, payload: PurgeSettingsPayload):
        await require_guild_access(request, guild_id)
        controls = bot.access_manager.controls
        allowed_limit = min(int(payload.limit), controls.FREE_PURGE_LIMIT_CAP)
        updated_limit = controls.set_purge_limit(guild_id, allowed_limit)
        await log_dashboard_event(
            request,
            guild_id,
            title="Dashboard Purge Limit Updated",
            description="Updated the maximum cleanup size from the dashboard.",
            fields=[("Purge Limit", str(min(updated_limit, controls.FREE_PURGE_LIMIT_CAP)), False)],
        )
        return JSONResponse(purge_settings_summary(guild_id) | {"limit": min(updated_limit, controls.FREE_PURGE_LIMIT_CAP)})

    @app.get("/api/guilds/{guild_id}/moderation-settings")
    async def get_moderation_settings(request: Request, guild_id: int):
        await require_guild_access(request, guild_id)
        return JSONResponse(moderation_settings_summary(guild_id))

    @app.post("/api/guilds/{guild_id}/moderation-settings")
    async def update_moderation_settings(request: Request, guild_id: int, payload: ModerationSettingsPayload):
        await require_guild_access(request, guild_id)
        updated = bot.access_manager.controls.set_moderation_settings(
            guild_id,
            confirmation_enabled=payload.confirmation_enabled,
            default_timeout_minutes=payload.default_timeout_minutes,
        )
        await log_dashboard_event(
            request,
            guild_id,
            title="Dashboard Moderation Flow Updated",
            description="Updated moderation confirmations and default timeout length.",
            fields=[
                ("Confirmations", "On" if updated["confirmation_enabled"] else "Off", True),
                ("Default Timeout", f"{updated['default_timeout_minutes']} minutes", True),
            ],
        )
        return JSONResponse(updated)

    return app


class DashboardServer:
    def __init__(self, bot, command_controls, command_logs):
        self.bot = bot
        self.command_controls = command_controls
        self.command_logs = command_logs
        self._thread = None

    async def start(self):
        self._thread = start_dashboard_server(self.bot)

    async def stop(self):
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)


def start_dashboard_server(bot) -> threading.Thread:
    port = resolve_dashboard_port()
    host = resolve_dashboard_host()
    app = create_dashboard_app(bot)
    config = uvicorn.Config(app=app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config=config)

    thread = threading.Thread(target=server.run, daemon=True, name="servercore-dashboard")
    thread.start()
    return thread
