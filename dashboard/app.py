from __future__ import annotations

import asyncio
import os
import threading
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode

import httpx
import uvicorn
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

    async def fetch_user_guild_member_roles(access_token: str, guild_id: int) -> set[int]:
        try:
            member = await fetch_discord_resource(access_token, f"/users/@me/guilds/{guild_id}/member")
        except Exception:
            return set()

        if not isinstance(member, dict):
            return set()

        return {
            int(role_id)
            for role_id in member.get("roles", [])
            if str(role_id).isdigit()
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

        premium_enabled = any(command["tier"] == PREMIUM_TIER for command in build_command_catalog(bot))
        manageable_guilds: list[dict] = []
        for guild in discord_guilds:
            permissions = int(guild.get("permissions", 0))
            guild_id = int(guild["id"])
            owner = bool(guild.get("owner"))
            can_manage_guild = owner or bool(permissions & MANAGE_GUILD or permissions & ADMINISTRATOR)
            editor_role_ids = set(bot.access_manager.controls.get_dashboard_editor_roles(guild_id))
            has_editor_role = False
            if editor_role_ids and not can_manage_guild:
                has_editor_role = bool(editor_role_ids & await fetch_user_guild_member_roles(access_token, guild_id))

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
                    "premium_enabled": premium_enabled,
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
    ) -> tuple[dict, list[dict]]:
        await require_user(request)
        guilds = await load_user_guilds(request)
        selected = next((guild for guild in guilds if guild["id"] == guild_id), None)
        if selected is None:
            raise HTTPException(status_code=403, detail="Guild access denied")
        if not selected.get("bot_installed"):
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

    def dashboard_access_summary(guild_id: int) -> dict:
        role_lookup = {role["id"]: role["name"] for role in guild_roles(guild_id)}
        editor_role_ids = bot.access_manager.controls.get_dashboard_editor_roles(guild_id)
        return {
            "editor_role_ids": editor_role_ids,
            "editor_role_names": [role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in editor_role_ids],
        }

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

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "guilds": len(bot.guilds) if bot else 0}

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        user = session_user(request)
        if user:
            return RedirectResponse(url="/servers", status_code=302)

        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
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
        return templates.TemplateResponse(
            request,
            "guilds.html",
            {
                "request": request,
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
                "scope": "identify guilds guilds.members.read",
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
        if session_user(request) is None:
            return RedirectResponse(url="/", status_code=302)
        selected_guild, guilds = await require_guild_access(request, guild_id)
        roles = guild_roles(guild_id)
        commands, module_cards = guild_command_rows(guild_id)
        logs = bot.command_logs.list_for_guild(guild_id, 80) if hasattr(bot, "command_logs") else []
        access_summary = dashboard_access_summary(guild_id)

        stats = {
            "commands": len(commands),
            "disabled": len([command for command in commands if not command["enabled"]]),
            "restricted": len([command for command in commands if command["allowed_role_ids"]]),
            "roles": len(roles),
            "modules": len(module_cards),
        }

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "request": request,
                "bot_name": bot.user.name if getattr(bot, "user", None) else "ServerCore",
                "user": session_user(request),
                "guilds": guilds,
                "selected_guild": selected_guild,
                "commands": commands,
                "sections": module_cards,
                "modules": module_cards,
                "roles": roles,
                "logs": logs,
                "stats": stats,
                "dashboard_base_url": dashboard_base_url,
                "dashboard_editor_role_ids": access_summary["editor_role_ids"],
                "dashboard_editor_role_names": access_summary["editor_role_names"],
                "can_manage_editor_roles": selected_guild["can_manage_editor_roles"],
            },
        )

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
        return JSONResponse(
            {
                "command_name": payload.command_name,
                "policy": policy,
                "allowed_role_names": [role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in policy["allowed_role_ids"]],
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
        return JSONResponse(
            {
                "editor_role_ids": editor_role_ids,
                "editor_role_names": [role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in editor_role_ids],
            }
        )

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
