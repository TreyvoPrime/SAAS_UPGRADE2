from __future__ import annotations

import os
import threading
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

MANAGE_GUILD = 0x20
ADMINISTRATOR = 0x08
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


class CommandPolicyPayload(BaseModel):
    command_name: str = Field(min_length=1)
    enabled: bool | None = None
    allowed_role_ids: list[int] | None = None


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

    discord_client_id = os.getenv("DISCORD_CLIENT_ID") or os.getenv("DISCORD_APP_ID")
    discord_client_secret = os.getenv("DISCORD_CLIENT_SECRET")
    dashboard_base_url = os.getenv("DASHBOARD_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

    def oauth_ready() -> bool:
        return bool(discord_client_id and discord_client_secret)

    def build_redirect_uri() -> str:
        return f"{dashboard_base_url}/auth/callback"

    async def fetch_discord_token(code: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://discord.com/api/oauth2/token",
                data={
                    "client_id": discord_client_id,
                    "client_secret": discord_client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": build_redirect_uri(),
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
            response.raise_for_status()
            return response.json()

    def session_user(request: Request) -> dict | None:
        return request.session.get("discord_user")

    async def load_user_guilds(request: Request) -> list[dict]:
        access_token = request.session.get("access_token")
        if not access_token:
            return []

        discord_guilds = await fetch_discord_resource(access_token, "/users/@me/guilds")
        manageable_guilds: list[dict] = []
        has_premium = any(command.tier == "premium" for command in bot.command_inventory)

        for guild in discord_guilds:
            permissions = int(guild.get("permissions", 0))
            if not (permissions & MANAGE_GUILD or permissions & ADMINISTRATOR):
                continue

            bot_guild = bot.get_guild(int(guild["id"]))
            if bot_guild is None:
                continue

            manageable_guilds.append(
                {
                    "id": int(guild["id"]),
                    "name": guild["name"],
                    "icon": guild.get("icon"),
                    "member_count": bot_guild.member_count or len(bot_guild.members),
                    "tier": "Premium" if has_premium else "Free",
                }
            )

        manageable_guilds.sort(key=lambda guild: guild["name"].lower())
        return manageable_guilds

    async def require_guild_access(request: Request, guild_id: int) -> tuple[dict, list[dict]]:
        user = session_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Login required")

        guilds = await load_user_guilds(request)
        selected = next((guild for guild in guilds if guild["id"] == guild_id), None)
        if selected is None:
            raise HTTPException(status_code=403, detail="Guild access denied")

        return selected, guilds

    def guild_roles(guild_id: int) -> list[dict]:
        guild = bot.get_guild(guild_id)
        if guild is None:
            return []

        roles = [
            {"id": role.id, "name": role.name, "position": role.position}
            for role in guild.roles
            if role.name != "@everyone"
        ]
        roles.sort(key=lambda role: role["position"], reverse=True)
        return roles

    def guild_command_rows(guild_id: int) -> list[dict]:
        commands = bot.command_inventory.to_dicts()
        policies = bot.policy_store.build_dashboard_view(
            guild_id,
            [command["name"] for command in commands],
        )
        roles_by_id = {role["id"]: role["name"] for role in guild_roles(guild_id)}

        rows = []
        for command in commands:
            policy = policies[command["name"]]
            rows.append(
                {
                    **command,
                    "enabled": policy["enabled"],
                    "allowed_role_ids": policy["allowed_role_ids"],
                    "allowed_role_names": [
                        roles_by_id.get(role_id, f"Deleted role ({role_id})")
                        for role_id in policy["allowed_role_ids"]
                    ],
                }
            )

        return rows

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "guilds": len(bot.guilds)}

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        user = session_user(request)
        guilds = await load_user_guilds(request) if user else []

        if user and guilds:
            return RedirectResponse(url=f"/dashboard/{guilds[0]['id']}", status_code=302)

        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "oauth_ready": oauth_ready(),
                "user": user,
                "guilds": guilds,
            },
        )

    @app.get("/login")
    async def login():
        if not oauth_ready():
            raise HTTPException(status_code=500, detail="Discord OAuth is not configured")

        query = urlencode(
            {
                "client_id": discord_client_id,
                "redirect_uri": build_redirect_uri(),
                "response_type": "code",
                "scope": "identify guilds",
                "prompt": "consent",
            }
        )
        return RedirectResponse(url=f"https://discord.com/oauth2/authorize?{query}", status_code=302)

    @app.get("/auth/callback")
    async def auth_callback(request: Request, code: str):
        if not oauth_ready():
            raise HTTPException(status_code=500, detail="Discord OAuth is not configured")

        token_data = await fetch_discord_token(code)
        user_data = await fetch_discord_resource(token_data["access_token"], "/users/@me")

        request.session["access_token"] = token_data["access_token"]
        request.session["discord_user"] = {
            "id": user_data["id"],
            "username": user_data["username"],
            "global_name": user_data.get("global_name"),
            "avatar": user_data.get("avatar"),
        }
        return RedirectResponse(url="/", status_code=302)

    @app.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse(url="/", status_code=302)

    @app.get("/dashboard/{guild_id}", response_class=HTMLResponse)
    async def dashboard(request: Request, guild_id: int):
        selected_guild, guilds = await require_guild_access(request, guild_id)
        commands = guild_command_rows(guild_id)
        roles = guild_roles(guild_id)
        logs = bot.usage_logger.recent_logs(guild_id, limit=75)

        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "user": session_user(request),
                "guilds": guilds,
                "selected_guild": selected_guild,
                "commands": commands,
                "roles": roles,
                "logs": logs,
                "counts": {
                    "total": len(commands),
                    "free": len([command for command in commands if command["tier"] == "free"]),
                    "premium": len([command for command in commands if command["tier"] == "premium"]),
                    "disabled": len([command for command in commands if not command["enabled"]]),
                },
            },
        )

    @app.get("/api/guilds/{guild_id}/logs")
    async def guild_logs(request: Request, guild_id: int, limit: int = 75):
        await require_guild_access(request, guild_id)
        return JSONResponse(bot.usage_logger.recent_logs(guild_id, limit=min(limit, 150)))

    @app.post("/api/guilds/{guild_id}/command-policy")
    async def update_command_policy(request: Request, guild_id: int, payload: CommandPolicyPayload):
        await require_guild_access(request, guild_id)

        if bot.command_inventory.get(payload.command_name) is None:
            raise HTTPException(status_code=404, detail="Unknown command")

        policy = bot.policy_store.get_command_policy(guild_id, payload.command_name)

        if payload.enabled is not None:
            policy = bot.policy_store.set_enabled(guild_id, payload.command_name, payload.enabled)

        if payload.allowed_role_ids is not None:
            policy = bot.policy_store.set_allowed_roles(
                guild_id,
                payload.command_name,
                payload.allowed_role_ids,
            )

        return JSONResponse(
            {
                "command_name": payload.command_name,
                "policy": policy,
            }
        )

    return app


def start_dashboard_server(bot) -> threading.Thread:
    port = int(os.getenv("DASHBOARD_PORT", "8000"))
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    app = create_dashboard_app(bot)
    config = uvicorn.Config(app=app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config=config)

    thread = threading.Thread(target=server.run, daemon=True, name="servercore-dashboard")
    thread.start()
    return thread
