from __future__ import annotations

import asyncio
import os
import threading
import traceback
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

from core.command_catalog import build_command_catalog

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

    discord_client_id = os.getenv("DISCORD_CLIENT_ID") or os.getenv("DISCORD_APP_ID") or "1487599032170975292"
    discord_client_secret = os.getenv("DISCORD_CLIENT_SECRET")
    dashboard_base_url = os.getenv("DASHBOARD_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

    def oauth_ready() -> bool:
        return bool(discord_client_id and discord_client_secret)

    def build_redirect_uri() -> str:
        return f"{dashboard_base_url}/auth/callback"

    async def fetch_discord_token(code: str) -> dict:
        try:
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
        except Exception:
            raise HTTPException(status_code=500, detail="OAuth token fetch failed")

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

    def session_user(request: Request) -> dict | None:
        return request.session.get("discord_user")

    async def load_user_guilds(request: Request) -> list[dict]:
        access_token = request.session.get("access_token")
        if not access_token:
            return []
        try:
            discord_guilds = await fetch_discord_resource(access_token, "/users/@me/guilds")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                await asyncio.sleep(5)
                discord_guilds = await fetch_discord_resource(access_token, "/users/@me/guilds")
            else:
                discord_guilds = []
        except:
            discord_guilds = []
        manageable_guilds: list[dict] = []
        try:
            commands = build_command_catalog(bot)
            has_premium = any(c["tier"] == "Premium" for c in commands)
        except:
            has_premium = False

        for guild in discord_guilds:
            try:
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
            except:
                continue

        manageable_guilds.sort(key=lambda g: g["name"].lower())
        return manageable_guilds

    async def require_guild_access(request: Request, guild_id: int) -> tuple[dict, list[dict]]:
        user = session_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Login required")

        guilds = await load_user_guilds(request)
        selected = next((g for g in guilds if g["id"] == guild_id), None)
        if selected is None:
            raise HTTPException(status_code=403, detail="Guild access denied")

        return selected, guilds

    def guild_roles(guild_id: int) -> list[dict]:
        try:
            guild = bot.get_guild(guild_id)
            if guild is None:
                return []
            roles = [
                {"id": role.id, "name": role.name, "position": role.position}
                for role in guild.roles
                if role.name != "@everyone"
            ]
            roles.sort(key=lambda r: r["position"], reverse=True)
            return roles
        except:
            return []

    def guild_command_rows(guild_id: int) -> list[dict]:
        try:
            commands = build_command_catalog(bot)
        except:
            commands = []
        roles_list = guild_roles(guild_id)
        roles_by_id = {r["id"]: r["name"] for r in roles_list}
        rows = []
        for command in commands:
            try:
                policy = bot.access_manager.controls.get_policy(guild_id, command["name"])
            except:
                policy = {"enabled": True, "allowed_role_ids": []}
            rows.append({
                **command,
                "enabled": policy["enabled"],
                "allowed_role_ids": policy["allowed_role_ids"] or [],
                "allowed_role_names": [roles_by_id.get(rid, f"Deleted ({rid})") for rid in policy["allowed_role_ids"]],
                "allowed_roles": [{"name": roles_by_id.get(rid, f"Deleted ({rid})")} for rid in policy["allowed_role_ids"]],
            })
        return rows

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "guilds": len(bot.guilds) if bot else 0}

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
        try:
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
        except:
            raise HTTPException(status_code=500, detail="OAuth callback failed")

    @app.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse(url="/", status_code=302)

    @app.get("/dashboard/{guild_id}", response_class=HTMLResponse)
    async def dashboard(request: Request, guild_id: int):
        try:
            selected_guild, guilds = await require_guild_access(request, guild_id)
            commands = guild_command_rows(guild_id)
            roles = guild_roles(guild_id)
            logs = bot.command_logs.list_for_guild(guild_id, 75) if hasattr(bot, 'command_logs') else []

            counts = {
                "total": len(commands),
                "free": len([c for c in commands if c.get("tier") == "free"]),
                "premium": len([c for c in commands if c.get("tier") == "premium"]),
                "disabled": len([c for c in commands if not c.get("enabled", True)]),
            }
            stats = {
                "commands": counts["total"],
                "disabled": counts["disabled"],
                "restricted": len([c for c in commands if c.get("allowed_role_ids")]),
                "roles": len(roles),
            }

            return templates.TemplateResponse(
                "dashboard.html",
                {
                    "request": request,
                    "user": session_user(request),
                    "guilds": guilds,
                    "selected_guild": selected_guild,
                    "guild": selected_guild,
                    "commands": commands,
                    "roles": roles,
                    "logs": logs,
                    "counts": counts,
                    "stats": stats,
                },
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Dashboard load failed: {str(e)}")

    @app.get("/api/guilds/{guild_id}/logs")
    async def guild_logs(request: Request, guild_id: int, limit: int = 75):
        await require_guild_access(request, guild_id)
        logs = bot.command_logs.list_for_guild(guild_id, min(limit, 150)) if hasattr(bot, 'command_logs') else []
        return JSONResponse(logs)

    @app.post("/api/guilds/{guild_id}/command-policy")
    async def update_command_policy(request: Request, guild_id: int, payload: CommandPolicyPayload):
        await require_guild_access(request, guild_id)

        try:
            commands = build_command_catalog(bot)
            if not any(c["name"] == payload.command_name for c in commands):
                raise HTTPException(status_code=404, detail="Unknown command")

            policy = bot.access_manager.controls.get_policy(guild_id, payload.command_name)
            if payload.enabled is not None:
                policy = bot.access_manager.controls.set_enabled(guild_id, payload.command_name, payload.enabled)
            if payload.allowed_role_ids is not None:
                policy = bot.access_manager.controls.set_roles(guild_id, payload.command_name, payload.allowed_role_ids)

            return JSONResponse({
                "command_name": payload.command_name,
                "policy": policy,
            })
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Policy update failed: {str(e)}")

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
    port = int(os.getenv("DASHBOARD_PORT", "8000"))
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    app = create_dashboard_app(bot)
    config = uvicorn.Config(app=app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config=config)

    thread = threading.Thread(target=server.run, daemon=True, name="servercore-dashboard")
    thread.start()
    return thread

