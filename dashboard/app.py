from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
import uvicorn
import discord
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from core.billing import BillingStore, FREE_PREMIUM_COMPARISON_ROWS, PREMIUM_PRICE_DISPLAY
from core.command_catalog import FREE_TIER, PREMIUM_TIER, build_command_catalog
from core.greetings import (
    DEFAULT_JOIN_DM_MESSAGE,
    DEFAULT_LEAVE_MESSAGE,
    DEFAULT_WELCOME_MESSAGE,
)
from core.premium import PREMIUM_FEATURES, TIER_FREE, TIER_PREMIUM, tier_label

MANAGE_GUILD = 0x20
ADMINISTRATOR = 0x08
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_SESSION_TTL_SECONDS = 60 * 60 * 8
DEFAULT_GUILD_CACHE_TTL_SECONDS = 30


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
    enabled: bool | None = None
    message: str | None = Field(default=None, max_length=1500)


class SupportSettingsPayload(BaseModel):
    issue_types: list[str] = Field(default_factory=list, max_length=20)
    command_channel_id: int | str | None = None


class PurgeSettingsPayload(BaseModel):
    limit: int = Field(ge=1, le=2000)
    default_mode: str = Field(default="all", min_length=1)
    include_pinned_default: bool = False


class ModerationSettingsPayload(BaseModel):
    confirmation_enabled: bool
    default_timeout_minutes: int = Field(ge=1, le=40320)


class AlertSettingsPayload(BaseModel):
    confirmation_enabled: bool
    skip_in_voice_default: bool
    only_offline_default: bool
    include_bots_default: bool
    cooldown_seconds: int = Field(ge=15, le=900)


class SubscriptionTierPayload(BaseModel):
    tier: str = Field(min_length=1)


class ConfigTemplatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class ConfigImportPayload(BaseModel):
    snapshot: dict


class SetupWizardPayload(BaseModel):
    moderation_confirmation_enabled: bool
    default_timeout_minutes: int = Field(ge=1, le=40320)
    moderation_allow_everyone: bool = False
    moderation_role_ids: list[int] = Field(default_factory=list)
    support_allow_everyone: bool = False
    support_role_ids: list[int] = Field(default_factory=list)
    community_allow_everyone: bool = False
    community_role_ids: list[int] = Field(default_factory=list)
    autorole_role_ids: list[int] = Field(default_factory=list)
    welcome_channel_id: int | str | None = None
    welcome_message: str | None = Field(default=None, max_length=1500)
    leave_channel_id: int | str | None = None
    leave_message: str | None = Field(default=None, max_length=1500)
    join_dm_enabled: bool = False
    join_dm_message: str | None = Field(default=None, max_length=1500)
    support_issue_types: list[str] = Field(default_factory=list, max_length=20)
    support_command_channel_id: int | str | None = None


def resolve_dashboard_host() -> str:
    return os.getenv("DASHBOARD_HOST") or os.getenv("HOST") or "0.0.0.0"


def resolve_dashboard_port() -> int:
    raw_value = os.getenv("PORT") or os.getenv("DASHBOARD_PORT") or "8000"
    try:
        port = int(raw_value)
    except (TypeError, ValueError) as error:
        raise RuntimeError("PORT or DASHBOARD_PORT must be a valid integer.") from error
    if not (1 <= port <= 65535):
        raise RuntimeError("PORT or DASHBOARD_PORT must be between 1 and 65535.")
    return port


def resolve_dashboard_base_url(host: str, port: int) -> str:
    explicit = os.getenv("DASHBOARD_BASE_URL")
    if explicit:
        return explicit.rstrip("/")

    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
    if railway_domain:
        return f"https://{railway_domain}".rstrip("/")

    public_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{public_host}:{port}".rstrip("/")


def resolve_dashboard_secret_key(dashboard_base_url: str) -> str:
    secret_key = os.getenv("DASHBOARD_SECRET_KEY")
    if secret_key:
        if len(secret_key) < 32:
            raise RuntimeError("DASHBOARD_SECRET_KEY must be at least 32 characters long.")
        return secret_key

    parsed = urlparse(dashboard_base_url)
    is_local = parsed.hostname in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}
    if is_local:
        return "servercore-local-development-session-secret"
    raise RuntimeError("DASHBOARD_SECRET_KEY is required for non-local dashboard deployments.")


class DashboardSessionStore:
    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        guild_cache_ttl_seconds: int = DEFAULT_GUILD_CACHE_TTL_SECONDS,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.guild_cache_ttl_seconds = guild_cache_ttl_seconds
        self._sessions: dict[str, dict] = {}

    def _now(self) -> float:
        return time.time()

    def _prune(self) -> None:
        now = self._now()
        expired = [
            session_id
            for session_id, payload in self._sessions.items()
            if float(payload.get("expires_at", 0)) <= now
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)

    def create_session(self, *, access_token: str, user: dict) -> str:
        self._prune()
        session_id = secrets.token_urlsafe(32)
        self._sessions[session_id] = {
            "access_token": str(access_token),
            "user": dict(user),
            "expires_at": self._now() + self.ttl_seconds,
            "guild_cache": None,
            "guild_cache_expires_at": 0.0,
        }
        return session_id

    def get_session(self, session_id: str | None) -> dict | None:
        self._prune()
        if not session_id:
            return None
        payload = self._sessions.get(session_id)
        if payload is None:
            return None
        payload["expires_at"] = self._now() + self.ttl_seconds
        return payload

    def get_user(self, session_id: str | None) -> dict | None:
        payload = self.get_session(session_id)
        return dict(payload["user"]) if payload else None

    def get_access_token(self, session_id: str | None) -> str | None:
        payload = self.get_session(session_id)
        return str(payload["access_token"]) if payload else None

    def get_cached_guilds(self, session_id: str | None) -> list[dict] | None:
        payload = self.get_session(session_id)
        if payload is None:
            return None
        if float(payload.get("guild_cache_expires_at", 0)) <= self._now():
            return None
        cached = payload.get("guild_cache")
        return list(cached) if isinstance(cached, list) else None

    def set_cached_guilds(self, session_id: str | None, guilds: list[dict]) -> None:
        payload = self.get_session(session_id)
        if payload is None:
            return
        payload["guild_cache"] = list(guilds)
        payload["guild_cache_expires_at"] = self._now() + self.guild_cache_ttl_seconds

    def clear_cached_guilds(self, session_id: str | None) -> None:
        payload = self.get_session(session_id)
        if payload is None:
            return
        payload["guild_cache"] = None
        payload["guild_cache_expires_at"] = 0.0

    def delete_session(self, session_id: str | None) -> None:
        if session_id:
            self._sessions.pop(session_id, None)


def create_dashboard_app(bot) -> FastAPI:
    dashboard_host = resolve_dashboard_host()
    dashboard_port = resolve_dashboard_port()
    dashboard_base_url = resolve_dashboard_base_url(dashboard_host, dashboard_port)
    session_secret_key = resolve_dashboard_secret_key(dashboard_base_url)
    secure_sessions = dashboard_base_url.startswith("https://")

    app = FastAPI(title="ServerCore Dashboard")
    app.state.dashboard_sessions = DashboardSessionStore()
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret_key,
        same_site="lax",
        https_only=secure_sessions,
        max_age=DEFAULT_SESSION_TTL_SECONDS,
        session_cookie="servercore_dashboard_session",
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    def render_template(name: str, request: Request, context: dict) -> HTMLResponse:
        merged_context = {"request": request, **context}
        template = templates.get_template(name)
        return HTMLResponse(template.render(merged_context))

    discord_client_id = os.getenv("DISCORD_CLIENT_ID") or os.getenv("DISCORD_APP_ID")
    discord_client_secret = os.getenv("DISCORD_CLIENT_SECRET")
    redirect_uri = os.getenv("DISCORD_REDIRECT_URI") or f"{dashboard_base_url}/auth/callback"
    install_permissions = os.getenv("DISCORD_INSTALL_PERMISSIONS", "8")
    session_store: DashboardSessionStore = app.state.dashboard_sessions
    billing_store: BillingStore = getattr(bot, "billing_store", None) or BillingStore()
    setattr(bot, "billing_store", billing_store)
    if hasattr(bot, "command_controls") and hasattr(bot.command_controls, "attach_billing_store"):
        bot.command_controls.attach_billing_store(billing_store)

    def oauth_ready() -> bool:
        return bool(discord_client_id and discord_client_secret)

    def stripe_secret_key_value() -> str | None:
        return os.getenv("STRIPE_SECRET_KEY")

    def stripe_webhook_secret_value() -> str | None:
        return os.getenv("STRIPE_WEBHOOK_SECRET")

    def stripe_premium_price_id_value() -> str | None:
        return os.getenv("STRIPE_PREMIUM_PRICE_ID")

    def stripe_checkout_ready() -> bool:
        return bool(stripe_secret_key_value() and stripe_premium_price_id_value())

    def stripe_billing_ready() -> bool:
        return bool(stripe_secret_key_value() and stripe_premium_price_id_value() and stripe_webhook_secret_value())

    def load_stripe_sdk():
        try:
            import stripe  # type: ignore
        except ImportError as error:
            raise HTTPException(status_code=503, detail="Stripe support is not installed on this deployment.") from error
        secret_key = stripe_secret_key_value()
        if not secret_key:
            raise HTTPException(status_code=503, detail="Stripe billing is not configured on this deployment.")
        stripe.api_key = secret_key
        return stripe

    def stripe_object_id(value) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            candidate = value.get("id")
            return str(candidate) if candidate else None
        candidate = getattr(value, "id", None)
        return str(candidate) if candidate else None

    def stripe_object_value(value, key: str, default=None):
        if value is None:
            return default
        if isinstance(value, dict):
            return value.get(key, default)
        return getattr(value, key, default)

    def session_id(request: Request) -> str | None:
        return request.session.get("dashboard_session_id")

    def session_user(request: Request) -> dict | None:
        return session_store.get_user(session_id(request))

    def session_user_id(request: Request) -> int | None:
        user = session_user(request) or {}
        raw_value = user.get("id")
        if raw_value is None or not str(raw_value).isdigit():
            return None
        return int(str(raw_value))

    def session_access_token(request: Request) -> str | None:
        return session_store.get_access_token(session_id(request))

    def clear_session(request: Request) -> None:
        session_store.delete_session(session_id(request))
        request.session.clear()

    def billing_summary(user_id: int | None) -> dict:
        subscription = billing_store.get_user_subscription(user_id)
        period_end = subscription.get("current_period_end")
        return {
            **subscription,
            "billing_ready": stripe_billing_ready(),
            "checkout_ready": stripe_checkout_ready(),
            "price_display": PREMIUM_PRICE_DISPLAY,
            "manage_billing_available": bool(subscription.get("stripe_customer_id")) and stripe_checkout_ready(),
            "period_end_label": (period_end or "")[:10] if period_end else None,
            "active_guild_count": len(subscription.get("active_guild_ids", [])),
        }

    def _guild_icon_url(raw_guild: dict, live_guild) -> str:
        if live_guild and getattr(live_guild, "icon", None):
            return live_guild.icon.url

        icon_hash = raw_guild.get("icon")
        if icon_hash:
            return f"https://cdn.discordapp.com/icons/{raw_guild['id']}/{icon_hash}.png?size=256"

        guild_id = int(raw_guild["id"])
        return f"https://cdn.discordapp.com/embed/avatars/{guild_id % 5}.png"

    def build_install_url(guild_id: int) -> str:
        if not discord_client_id:
            return "/"
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

    def _request_origin_matches_dashboard(request: Request) -> bool:
        dashboard_origin = urlparse(dashboard_base_url)
        expected_origin = f"{dashboard_origin.scheme}://{dashboard_origin.netloc}"
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        if origin:
            return origin.rstrip("/") == expected_origin.rstrip("/")
        if referer:
            parsed_referer = urlparse(referer)
            referer_origin = f"{parsed_referer.scheme}://{parsed_referer.netloc}"
            return referer_origin.rstrip("/") == expected_origin.rstrip("/")
        return True

    async def require_same_origin(request: Request) -> None:
        if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
            return
        if not _request_origin_matches_dashboard(request):
            raise HTTPException(status_code=403, detail="Request origin denied")

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
            return await coro
        if getattr(bot_loop, "is_closed", lambda: False)():
            raise HTTPException(status_code=503, detail="Bot event loop is closed")
        if bot_loop == current_loop:
            return await coro

        future = asyncio.run_coroutine_threadsafe(coro, bot_loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=15)
        except TimeoutError as error:
            future.cancel()
            raise HTTPException(status_code=504, detail="Bot worker timed out while processing that request") from error

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

    def sync_subscription_record(
        user_id: int,
        *,
        customer_id: str | None = None,
        subscription_id: str | None = None,
        status: str | None = None,
        current_period_end: int | float | str | None = None,
        cancel_at_period_end: bool | None = None,
        email: str | None = None,
    ) -> dict:
        return billing_store.upsert_subscription(
            user_id,
            stripe_customer_id=customer_id,
            stripe_subscription_id=subscription_id,
            status=status,
            current_period_end=current_period_end,
            cancel_at_period_end=cancel_at_period_end,
            email=email,
        )

    def sync_subscription_from_object(
        user_id: int,
        subscription_object,
        *,
        customer_id: str | None = None,
        email: str | None = None,
    ) -> dict:
        return sync_subscription_record(
            user_id,
            customer_id=customer_id or stripe_object_id(stripe_object_value(subscription_object, "customer")),
            subscription_id=stripe_object_id(subscription_object),
            status=stripe_object_value(subscription_object, "status"),
            current_period_end=stripe_object_value(subscription_object, "current_period_end"),
            cancel_at_period_end=bool(stripe_object_value(subscription_object, "cancel_at_period_end", False)),
            email=email,
        )

    def sync_checkout_session_to_billing(checkout_session, *, expected_user_id: int | None = None) -> dict | None:
        raw_user_id = (
            stripe_object_value(checkout_session, "client_reference_id")
            or (stripe_object_value(checkout_session, "metadata", {}) or {}).get("discord_user_id")
        )
        if raw_user_id is None or not str(raw_user_id).isdigit():
            return None
        user_id = int(str(raw_user_id))
        if expected_user_id is not None and user_id != expected_user_id:
            return None

        customer_id = stripe_object_id(stripe_object_value(checkout_session, "customer"))
        subscription_object = stripe_object_value(checkout_session, "subscription")
        customer_details = stripe_object_value(checkout_session, "customer_details", {}) or {}
        email = None
        if isinstance(customer_details, dict):
            email = customer_details.get("email")
        else:
            email = getattr(customer_details, "email", None)

        billing_store.set_checkout_session(user_id, stripe_object_id(checkout_session))

        if subscription_object is None:
            return sync_subscription_record(user_id, customer_id=customer_id, email=email)

        if isinstance(subscription_object, str):
            stripe = load_stripe_sdk()
            subscription_object = stripe.Subscription.retrieve(subscription_object)

        return sync_subscription_from_object(user_id, subscription_object, customer_id=customer_id, email=email)

    def sync_subscription_event_to_billing(subscription_object) -> dict | None:
        customer_id = stripe_object_id(stripe_object_value(subscription_object, "customer"))
        subscription_id = stripe_object_id(subscription_object)
        metadata = stripe_object_value(subscription_object, "metadata", {}) or {}
        raw_user_id = None
        if isinstance(metadata, dict):
            raw_user_id = metadata.get("discord_user_id")
        else:
            raw_user_id = getattr(metadata, "discord_user_id", None)

        user_id = None
        if raw_user_id is not None and str(raw_user_id).isdigit():
            user_id = int(str(raw_user_id))
        if user_id is None:
            user_id = billing_store.find_user_id_by_customer_id(customer_id) or billing_store.find_user_id_by_subscription_id(subscription_id)
        if user_id is None:
            return None
        return sync_subscription_from_object(user_id, subscription_object, customer_id=customer_id)

    def sync_invoice_event_to_billing(invoice_object) -> dict | None:
        subscription_id = stripe_object_id(stripe_object_value(invoice_object, "subscription"))
        customer_id = stripe_object_id(stripe_object_value(invoice_object, "customer"))
        user_id = billing_store.find_user_id_by_customer_id(customer_id) or billing_store.find_user_id_by_subscription_id(subscription_id)
        if user_id is None:
            return None

        stripe = load_stripe_sdk()
        if subscription_id:
            subscription_object = stripe.Subscription.retrieve(subscription_id)
            return sync_subscription_from_object(user_id, subscription_object, customer_id=customer_id)
        return sync_subscription_record(user_id, customer_id=customer_id)

    def premium_enabled() -> bool:
        return any(command["tier"] == PREMIUM_TIER for command in build_command_catalog(bot))

    def subscription_summary(guild_id: int, viewer_user_id: int | None = None) -> dict:
        controls = bot.access_manager.controls
        tier = controls.get_subscription_tier(guild_id)
        is_premium = controls.is_premium_enabled(guild_id)
        guild = bot.get_guild(guild_id)
        viewer_billing = billing_summary(viewer_user_id)
        guild_assignment = billing_store.get_guild_assignment(guild_id)
        guild_name = guild.name if guild is not None else "This server"
        if viewer_billing["billing_ready"]:
            if is_premium:
                summary_copy = f"{guild_name} is on the Premium tier. Guardian and the advanced staff toolkit are active here."
            elif viewer_billing["is_active"]:
                summary_copy = "Premium is ready for this server. Turn it on here whenever you want Guardian and the advanced staff toolkit unlocked."
            else:
                summary_copy = "Premium unlocks Guardian and the advanced staff toolkit for this server."
        else:
            summary_copy = (
                f"{guild_name} is currently on the {tier_label(tier)} tier. Premium unlocks Guardian, advanced ticket tools, "
                "analytics, richer automation, config tools, and advanced giveaway rules."
            )

        if is_premium:
            state_title = "Premium is active"
            state_copy = "Guardian and advanced tools are unlocked for this server."
        elif viewer_billing["is_active"]:
            state_title = "Premium is ready"
            state_copy = "Premium is ready for this server. Turn it on here whenever you want to unlock Guardian and advanced tools."
        elif viewer_billing["billing_ready"]:
            state_title = "Free is active"
            state_copy = "Start Premium for this server, then come back here to unlock Guardian and advanced tools."
        else:
            state_title = "Free is active"
            state_copy = "Guardian and advanced tools stay locked until Premium is turned on."
        return {
            "tier": tier,
            "label": tier_label(tier),
            "is_premium": is_premium,
            "guild_name": guild_name,
            "guardian_locked": not is_premium,
            "feature_count": len(PREMIUM_FEATURES),
            "features": [{"key": key, "label": label} for key, label in PREMIUM_FEATURES.items()],
            "config_templates": controls.list_config_templates(guild_id),
            "billing_ready": viewer_billing["billing_ready"],
            "checkout_ready": viewer_billing["checkout_ready"],
            "viewer_has_premium": viewer_billing["is_active"],
            "viewer_status_label": viewer_billing["status_label"],
            "viewer_subscription_status": viewer_billing["status"],
            "viewer_period_end_label": viewer_billing["period_end_label"],
            "viewer_active_guild_count": viewer_billing["active_guild_count"],
            "viewer_can_manage_billing": viewer_billing["manage_billing_available"],
            "assigned_user_id": guild_assignment["premium_user_id"],
            "assigned_to_viewer": viewer_user_id is not None and guild_assignment["premium_user_id"] == viewer_user_id,
            "summary_copy": summary_copy,
            "state_title": state_title,
            "state_copy": state_copy,
            "billing_button_label": "Manage billing" if viewer_billing["manage_billing_available"] else "Premium plans",
            "premium_button_label": "Use Premium" if viewer_billing["is_active"] or not viewer_billing["billing_ready"] else "Get Premium",
        }

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
            "premium_enabled": bot.access_manager.controls.is_premium_enabled(guild.id),
            "bot_installed": True,
            "dashboard_url": f"/dashboard/{guild.id}",
            "install_url": build_install_url(guild.id),
            "owner": owner,
            "can_manage_guild": can_manage_guild,
            "can_edit_dashboard": can_edit_dashboard,
            "can_manage_editor_roles": can_manage_guild,
        }

    async def load_user_guilds(request: Request) -> list[dict]:
        access_token = session_access_token(request)
        user = session_user(request)
        if not access_token:
            return []
        if not user or not str(user.get("id", "")).isdigit():
            return []

        cached_guilds = session_store.get_cached_guilds(session_id(request))
        if cached_guilds is not None:
            return cached_guilds

        try:
            discord_guilds = await fetch_discord_resource(access_token, "/users/@me/guilds")
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {401, 403}:
                clear_session(request)
            return cached_guilds or []
        except Exception:
            return cached_guilds or []

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
                    "premium_enabled": bot.access_manager.controls.is_premium_enabled(guild_id),
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
        session_store.set_cached_guilds(session_id(request), manageable_guilds)
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

    def greeting_preview(
        template: str,
        guild_id: int,
        *,
        fallback: str = DEFAULT_WELCOME_MESSAGE,
        user_mention: str = "@new-member",
        user_name: str = "new-member",
        display_name: str = "New Member",
    ) -> str:
        guild = bot.get_guild(guild_id)
        guild_name = guild.name if guild is not None else "Your Server"
        member_count = guild.member_count if guild is not None and guild.member_count is not None else 0
        values = {
            "{user}": user_mention,
            "{user_name}": user_name,
            "{username}": user_name,
            "{display_name}": display_name,
            "{server}": guild_name,
            "{membercount}": str(member_count),
        }
        preview = str(template or "").strip()
        for token, replacement in values.items():
            preview = preview.replace(token, replacement)
        return preview or fallback

    def dashboard_access_summary(guild_id: int) -> dict:
        role_lookup = {role["id"]: role["name"] for role in guild_roles(guild_id)}
        editor_role_ids = bot.access_manager.controls.get_dashboard_editor_roles(guild_id)
        return {
            "editor_role_ids": editor_role_ids,
            "editor_role_names": [role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in editor_role_ids],
        }

    def purge_settings_summary(guild_id: int) -> dict:
        controls = bot.access_manager.controls
        settings = controls.get_purge_settings(guild_id)
        max_limit = controls.FREE_PURGE_LIMIT_CAP
        premium_limit = controls.PREMIUM_PURGE_LIMIT_CAP
        return {
            "limit": min(settings["limit"], max_limit),
            "default_mode": settings["default_mode"],
            "include_pinned_default": settings["include_pinned_default"],
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

    def autorole_settings_summary(guild_id: int) -> dict:
        controls = bot.access_manager.controls
        role_lookup = {role["id"]: role["name"] for role in guild_roles(guild_id)}
        autorole_role_ids = controls.get_autorole_role_ids(guild_id)
        return {
            "role_ids": autorole_role_ids,
            "role_names": [role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in autorole_role_ids],
        }

    def greetings_dashboard_summary(guild_id: int) -> dict:
        manager = getattr(bot, "greetings", None)
        channels = guild_text_channels(guild_id)
        channel_lookup = {channel["id"]: channel["label"] for channel in channels}
        if manager is not None and hasattr(manager, "get_dashboard_state"):
            summary = manager.get_dashboard_state(guild_id, channel_lookup)
            summary["welcome"]["default_message"] = DEFAULT_WELCOME_MESSAGE
            summary["leave"]["default_message"] = DEFAULT_LEAVE_MESSAGE
            summary["join_dm"]["default_message"] = DEFAULT_JOIN_DM_MESSAGE
            summary["join_dm"]["channel_name"] = (
                "Join guide DM is currently on." if summary["join_dm"]["enabled"] else "Join guide DM is currently off."
            )
            summary["welcome"]["preview"] = greeting_preview(summary["welcome"]["message"], guild_id, fallback=DEFAULT_WELCOME_MESSAGE)
            summary["leave"]["preview"] = greeting_preview(
                summary["leave"]["message"],
                guild_id,
                fallback=DEFAULT_LEAVE_MESSAGE,
                user_mention="@departing-member",
                user_name="departing-member",
                display_name="Departing Member",
            )
            summary["join_dm"]["preview"] = greeting_preview(summary["join_dm"]["message"], guild_id, fallback=DEFAULT_JOIN_DM_MESSAGE)
            return summary

        return {
            "welcome": {
                "channel_id": None,
                "channel_name": "Not configured",
                "message": "Hello {user}, welcome to {server}.",
                "enabled": False,
                "default_message": DEFAULT_WELCOME_MESSAGE,
                "preview": greeting_preview(DEFAULT_WELCOME_MESSAGE, guild_id, fallback=DEFAULT_WELCOME_MESSAGE),
            },
            "leave": {
                "channel_id": None,
                "channel_name": "Not configured",
                "message": "{user_name} left {server}.",
                "enabled": False,
                "default_message": DEFAULT_LEAVE_MESSAGE,
                "preview": greeting_preview(
                    DEFAULT_LEAVE_MESSAGE,
                    guild_id,
                    fallback=DEFAULT_LEAVE_MESSAGE,
                    user_mention="@departing-member",
                    user_name="departing-member",
                    display_name="Departing Member",
                ),
            },
            "join_dm": {
                "enabled": False,
                "message": "Welcome to {server}, {display_name}. Read the server guide and check the rules channel to get started.",
                "default_message": DEFAULT_JOIN_DM_MESSAGE,
                "preview": greeting_preview(DEFAULT_JOIN_DM_MESSAGE, guild_id, fallback=DEFAULT_JOIN_DM_MESSAGE),
                "channel_name": "Join guide DM is currently off.",
            },
            "placeholders": [
                {"token": "{user}", "label": "Mentions the member"},
                {"token": "{user_name}", "label": "Uses the member name"},
                {"token": "{display_name}", "label": "Uses the server nickname"},
                {"token": "{server}", "label": "Uses the server name"},
                {"token": "{membercount}", "label": "Uses the current member count"},
            ],
        }

    def alert_settings_summary(guild_id: int) -> dict:
        controls = bot.access_manager.controls
        settings = controls.get_alert_settings(guild_id)
        return {
            "confirmation_enabled": settings["confirmation_enabled"],
            "skip_in_voice_default": settings["skip_in_voice_default"],
            "only_offline_default": settings["only_offline_default"],
            "include_bots_default": settings["include_bots_default"],
            "cooldown_seconds": settings["cooldown_seconds"],
        }

    def support_dashboard_summary(guild_id: int) -> dict:
        ticket_store = getattr(bot, "ticket_store", None)
        if ticket_store is None:
            return {
                "issue_types": [],
                "issue_count": 0,
                "support_category_id": None,
                "support_category_name": "Not configured yet",
                "support_command_channel_id": None,
                "support_command_channel_name": "Any channel",
            }

        category_id = ticket_store.get_support_category_id(guild_id)
        command_channel_id = ticket_store.get_support_command_channel_id(guild_id)
        guild = bot.get_guild(guild_id)
        category_name = "Not configured yet"
        command_channel_name = "Any channel"
        if guild is not None and category_id:
            category = guild.get_channel(category_id)
            if category is not None:
                category_name = getattr(category, "name", "Configured")
        if guild is not None and command_channel_id:
            command_channel = guild.get_channel(command_channel_id)
            if command_channel is not None:
                command_channel_name = getattr(command_channel, "mention", f"#{getattr(command_channel, 'name', 'configured-channel')}")
        return {
            "issue_types": ticket_store.get_issue_types(guild_id),
            "issue_count": len(ticket_store.get_issue_types(guild_id)),
            "support_category_id": category_id,
            "support_category_name": category_name,
            "support_command_channel_id": command_channel_id,
            "support_command_channel_name": command_channel_name,
            "active_tickets": ticket_store.list_tickets(guild_id, status="open", limit=10),
            "analytics": ticket_store.analytics(guild_id) if hasattr(ticket_store, "analytics") else None,
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

    def analytics_summary(guild_id: int) -> dict:
        logs = bot.command_logs.list_for_guild(guild_id, 500) if hasattr(bot, "command_logs") else []
        case_store = getattr(bot, "case_store", None)
        ticket_store = getattr(bot, "ticket_store", None)
        giveaway_store = getattr(bot, "giveaway_store", None)
        guild = bot.get_guild(guild_id)
        cases = case_store.list_cases(guild_id, 500) if case_store is not None else []
        ticket_stats = ticket_store.analytics(guild_id) if ticket_store is not None and hasattr(ticket_store, "analytics") else {
            "total": 0,
            "open": 0,
            "closed": 0,
            "claimed": 0,
            "internal_note_count": 0,
            "priorities": {"low": 0, "normal": 0, "high": 0, "urgent": 0},
        }
        giveaways = giveaway_store.list_giveaways(guild_id, status=None, limit=500) if giveaway_store is not None else []
        command_entries = [entry for entry in logs if entry.get("kind") != "event"]
        command_counts: dict[str, int] = defaultdict(int)
        moderator_counts: dict[str, int] = defaultdict(int)
        for entry in command_entries:
            command_counts[str(entry.get("command") or "unknown")] += 1
            user_name = str(entry.get("user_name") or "Unknown")
            if entry.get("command") in {"warn", "timeout", "removetimeout", "kick", "ban"}:
                moderator_counts[user_name] += 1
        most_used_commands = sorted(command_counts.items(), key=lambda item: item[1], reverse=True)[:5]
        most_active_staff = sorted(moderator_counts.items(), key=lambda item: item[1], reverse=True)[:5]
        return {
            "member_count": getattr(guild, "member_count", 0) or 0,
            "command_count": len(command_entries),
            "event_count": len([entry for entry in logs if entry.get("kind") == "event"]),
            "case_count": len(cases),
            "ticket_total": ticket_stats["total"],
            "ticket_open": ticket_stats["open"],
            "giveaway_total": len(giveaways),
            "most_used_commands": most_used_commands,
            "most_active_staff": most_active_staff,
        }

    def premium_log_entries(
        guild_id: int,
        *,
        limit: int = 250,
        query: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        category: str | None = None,
        actor: str | None = None,
    ) -> list[dict]:
        if not hasattr(bot, "command_logs"):
            return []
        return bot.command_logs.list_for_guild(
            guild_id,
            max(1, min(limit, 1000)),
            query=query,
            kind=kind,
            status=status,
            category=category,
            actor=actor,
        )

    def premium_case_entries(
        guild_id: int,
        *,
        limit: int = 150,
        query: str | None = None,
        action: str | None = None,
    ) -> list[dict]:
        case_store = getattr(bot, "case_store", None)
        if case_store is None:
            return []
        cases = case_store.list_cases(guild_id, 1000)
        if query:
            needle = str(query).strip().lower()
            cases = [
                case for case in cases
                if needle in str(case.get("target_user_name") or "").lower()
                or needle in str(case.get("moderator_name") or "").lower()
                or needle in str(case.get("reason") or "").lower()
                or needle == str(case.get("case_id"))
                or any(needle in str(note.get("body") or "").lower() for note in case.get("notes", []))
            ]
        if action:
            action_key = str(action).strip().lower()
            cases = [case for case in cases if str(case.get("action") or "").lower() == action_key]
        return cases[: max(1, min(limit, 500))]

    def build_config_snapshot(guild_id: int) -> dict:
        return {
            "subscription_tier": bot.access_manager.controls.get_subscription_tier(guild_id),
            "dashboard_editor_role_ids": bot.access_manager.controls.get_dashboard_editor_roles(guild_id),
            "autorole_role_ids": bot.access_manager.controls.get_autorole_role_ids(guild_id),
            "purge_settings": purge_settings_summary(guild_id),
            "moderation_settings": moderation_settings_summary(guild_id),
            "alert_settings": alert_settings_summary(guild_id),
            "greetings": greetings_dashboard_summary(guild_id),
            "support": support_dashboard_summary(guild_id),
            "server_defense": bot.server_defense.get_dashboard_state(guild_id) if hasattr(bot, "server_defense") else {},
        }

    async def apply_config_snapshot(guild_id: int, snapshot: dict) -> None:
        controls = bot.access_manager.controls
        controls.set_subscription_tier(guild_id, str(snapshot.get("subscription_tier") or TIER_FREE))
        controls.set_dashboard_editor_roles(guild_id, [int(role_id) for role_id in snapshot.get("dashboard_editor_role_ids", []) if str(role_id).isdigit()])
        controls.set_autorole_role_ids(guild_id, [int(role_id) for role_id in snapshot.get("autorole_role_ids", []) if str(role_id).isdigit()])

        purge = snapshot.get("purge_settings") or {}
        controls.set_purge_settings(
            guild_id,
            limit=purge.get("limit"),
            default_mode=purge.get("default_mode"),
            include_pinned_default=purge.get("include_pinned_default"),
        )

        moderation = snapshot.get("moderation_settings") or {}
        controls.set_moderation_settings(
            guild_id,
            confirmation_enabled=moderation.get("confirmation_enabled"),
            default_timeout_minutes=moderation.get("default_timeout_minutes"),
        )

        alert = snapshot.get("alert_settings") or {}
        controls.set_alert_settings(
            guild_id,
            confirmation_enabled=alert.get("confirmation_enabled"),
            skip_in_voice_default=alert.get("skip_in_voice_default"),
            only_offline_default=alert.get("only_offline_default"),
            include_bots_default=alert.get("include_bots_default"),
            cooldown_seconds=alert.get("cooldown_seconds"),
        )

        greetings = snapshot.get("greetings") or {}
        manager = getattr(bot, "greetings", None)
        if manager is not None:
            welcome = greetings.get("welcome") or {}
            leave = greetings.get("leave") or {}
            join_dm = greetings.get("join_dm") or {}
            manager.set_welcome(guild_id, channel_id=welcome.get("channel_id"), message=welcome.get("message"))
            manager.set_leave(guild_id, channel_id=leave.get("channel_id"), message=leave.get("message"))
            manager.set_join_dm(guild_id, enabled=join_dm.get("enabled"), message=join_dm.get("message"))

        ticket_store = getattr(bot, "ticket_store", None)
        support = snapshot.get("support") or {}
        if ticket_store is not None:
            ticket_store.set_issue_types(guild_id, support.get("issue_types", []))
            ticket_store.set_support_command_channel_id(guild_id, support.get("support_command_channel_id"))

        server_defense = snapshot.get("server_defense") or {}
        if hasattr(bot, "server_defense") and isinstance(server_defense, dict):
            for feature_name, state in server_defense.items():
                if not isinstance(state, dict):
                    continue
                if feature_name == "active_count":
                    continue
                if feature_name not in {"linkblock", "inviteblock", "antispam", "antijoin", "mentionguard", "autofilter", "lockdown", "antiraid"}:
                    continue
                if hasattr(bot.server_defense.store, "patch_feature"):
                    bot.server_defense.store.patch_feature(guild_id, feature_name, **state)

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

    setup_command_groups = {
        "moderation": [
            "warn",
            "timeout",
            "removetimeout",
            "kick",
            "ban",
            "purge",
            "role add",
            "role remove",
            "role temp",
            "history",
            "staffnotes add",
            "staffnotes view",
            "staffnotes remove",
        ],
        "support": [
            "ticketissue add",
            "ticketissue remove",
            "ticketissue list",
            "ticketclaim",
            "ticketpriority",
            "tickettranscript",
            "closeticket",
        ],
        "community": [
            "poll",
            "pollresults",
            "endpoll",
            "giveaway create",
            "giveaway end",
            "giveaway list",
            "giveaway reroll",
            "autofeed create",
            "autofeed edit",
            "autofeed delete",
            "autofeed pause",
            "autofeed resume",
            "remind",
        ],
    }

    def setup_role_group_summary(guild_id: int, group_name: str, role_lookup: dict[int, str]) -> dict:
        command_names = setup_command_groups[group_name]
        controls = bot.access_manager.controls
        group_role_ids: set[int] = set()
        allow_all = True
        for command_name in command_names:
            policy = controls.get_policy(guild_id, command_name)
            role_ids = set(policy["allowed_role_ids"])
            group_role_ids.update(role_ids)
            if policy.get("restrict_to_roles"):
                allow_all = False
        role_ids_sorted = sorted(group_role_ids)
        return {
            "role_ids": role_ids_sorted,
            "role_names": [role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in role_ids_sorted],
            "allow_all": allow_all,
            "command_names": command_names,
        }

    def setup_wizard_summary(guild_id: int) -> dict:
        role_lookup = {role["id"]: role["name"] for role in guild_roles(guild_id)}
        return {
            "completed": bot.access_manager.controls.is_setup_wizard_completed(guild_id),
            "moderation": setup_role_group_summary(guild_id, "moderation", role_lookup),
            "support": setup_role_group_summary(guild_id, "support", role_lookup),
            "community": setup_role_group_summary(guild_id, "community", role_lookup),
            "autorole": autorole_settings_summary(guild_id),
        }

    def guild_command_rows(guild_id: int) -> tuple[list[dict], list[dict]]:
        roles = guild_roles(guild_id)
        role_names = {role["id"]: role["name"] for role in roles}
        grouped: dict[str, list[dict]] = defaultdict(list)
        rows: list[dict] = []

        for command in build_command_catalog(bot):
            policy = bot.access_manager.controls.get_policy(guild_id, command["name"])
            allowed_role_ids = policy["allowed_role_ids"] or []
            restrict_to_roles = bool(policy.get("restrict_to_roles"))
            row = {
                **command,
                "slug": _slugify(command["name"]),
                "module_slug": _slugify(command["module"]),
                "tier_slug": command["tier"].lower(),
                "enabled": policy["enabled"],
                "restrict_to_roles": restrict_to_roles,
                "allowed_role_ids": allowed_role_ids,
                "allowed_role_names": [role_names.get(role_id, f"Deleted ({role_id})") for role_id in allowed_role_ids],
                "allowed_role_count": len(allowed_role_ids),
                "role_summary": (
                    ", ".join(role_names.get(role_id, f"Deleted ({role_id})") for role_id in allowed_role_ids)
                    if allowed_role_ids
                    else "No roles selected"
                ) if restrict_to_roles else "All roles that already pass native Discord checks",
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
                    "restricted_count": len([command for command in commands if command["restrict_to_roles"]]),
                    "tier": PREMIUM_TIER if any(command["tier"] == PREMIUM_TIER for command in commands) else FREE_TIER,
                    "commands": sorted(commands, key=lambda item: item["name"]),
                }
            )

        rows.sort(key=lambda item: (item["module"].lower(), item["name"]))
        return rows, module_cards

    def help_catalog_sections() -> tuple[list[dict], dict[str, int]]:
        grouped: dict[str, list[dict]] = defaultdict(list)
        commands = build_command_catalog(bot)
        for command in commands:
            grouped[command["module"]].append(
                {
                    "name": command["name"],
                    "description": command["description"],
                    "tier": command["tier"],
                    "tier_slug": command["tier"].lower(),
                    "search_blob": f"{command['name']} {command['module']} {command['tier']} {command['description']}".lower(),
                }
            )

        sections: list[dict] = []
        for module_name, module_commands in sorted(grouped.items(), key=lambda item: item[0].lower()):
            sections.append(
                {
                    "name": module_name,
                    "slug": _slugify(module_name),
                    "count": len(module_commands),
                    "premium_count": sum(1 for command in module_commands if command["tier"] == PREMIUM_TIER),
                    "commands": sorted(module_commands, key=lambda item: item["name"]),
                }
            )

        return sections, {
            "total": len(commands),
            "modules": len(sections),
        }

    async def render_dashboard_view(request: Request, guild_id: int, current_view: str) -> HTMLResponse | RedirectResponse:
        if session_user(request) is None:
            return RedirectResponse(url="/", status_code=302)
        selected_guild, guilds = await require_guild_access(request, guild_id, require_bot_installed=False)
        if not selected_guild.get("bot_installed"):
            return RedirectResponse(url=selected_guild["install_url"], status_code=302)
        if current_view != "setup" and not bot.access_manager.controls.is_setup_wizard_completed(guild_id):
            return RedirectResponse(url=f"/dashboard/{guild_id}/setup", status_code=302)
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
        viewer_user_id = session_user_id(request)
        subscription = subscription_summary(guild_id, viewer_user_id)
        analytics = analytics_summary(guild_id) if subscription["is_premium"] else None
        purge_settings = purge_settings_summary(guild_id)
        moderation_settings = moderation_settings_summary(guild_id)
        alert_settings = alert_settings_summary(guild_id)

        stats = {
            "commands": len(commands),
            "disabled": len([command for command in commands if not command["enabled"]]),
            "restricted": len([command for command in commands if command["restrict_to_roles"]]),
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
                "subscription": subscription,
                "billing": billing_summary(viewer_user_id),
                "analytics": analytics,
                "purge_settings": purge_settings,
                "moderation_settings": moderation_settings,
                "alert_settings": alert_settings,
                "current_view": current_view,
            },
        )

    async def render_setup_wizard_view(request: Request, guild_id: int) -> HTMLResponse | RedirectResponse:
        if session_user(request) is None:
            return RedirectResponse(url="/", status_code=302)
        selected_guild, guilds = await require_guild_access(request, guild_id, require_bot_installed=False)
        if not selected_guild.get("bot_installed"):
            return RedirectResponse(url=selected_guild["install_url"], status_code=302)
        roles = guild_roles(guild_id)
        text_channels = guild_text_channels(guild_id)
        greetings_summary = greetings_dashboard_summary(guild_id)
        support_summary = support_dashboard_summary(guild_id)
        moderation_settings = moderation_settings_summary(guild_id)
        setup_summary = setup_wizard_summary(guild_id)
        viewer_user_id = session_user_id(request)
        subscription = subscription_summary(guild_id, viewer_user_id)
        return render_template(
            "setup.html",
            request,
            {
                "bot_name": bot.user.name if getattr(bot, "user", None) else "ServerCore",
                "user": session_user(request),
                "guilds": guilds,
                "selected_guild": selected_guild,
                "roles": roles,
                "text_channels": text_channels,
                "greetings_summary": greetings_summary,
                "support_summary": support_summary,
                "moderation_settings": moderation_settings,
                "setup_summary": setup_summary,
                "subscription": subscription,
                "billing": billing_summary(viewer_user_id),
                "current_view": "setup",
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
        viewer_user_id = session_user_id(request)
        return render_template(
            "guilds.html",
            request,
            {
                "bot_name": bot.user.name if getattr(bot, "user", None) else "ServerCore",
                "user": user,
                "guilds": guilds,
                "premium_count": premium_count,
                "billing": billing_summary(viewer_user_id),
            },
        )

    @app.get("/help", response_class=HTMLResponse)
    async def help_page(request: Request):
        user = session_user(request)
        if user is None:
            return RedirectResponse(url="/", status_code=302)
        sections, stats = help_catalog_sections()
        viewer_user_id = session_user_id(request)
        return render_template(
            "help.html",
            request,
            {
                "bot_name": bot.user.name if getattr(bot, "user", None) else "ServerCore",
                "user": user,
                "sections": sections,
                "stats": stats,
                "billing": billing_summary(viewer_user_id),
            },
        )

    @app.get("/terms", response_class=HTMLResponse)
    async def terms_page(request: Request):
        user = session_user(request)
        viewer_user_id = session_user_id(request)
        return render_template(
            "terms.html",
            request,
            {
                "bot_name": bot.user.name if getattr(bot, "user", None) else "ServerCore",
                "user": user,
                "billing": billing_summary(viewer_user_id),
            },
        )

    @app.get("/privacy", response_class=HTMLResponse)
    async def privacy_page(request: Request):
        user = session_user(request)
        viewer_user_id = session_user_id(request)
        return render_template(
            "privacy.html",
            request,
            {
                "bot_name": bot.user.name if getattr(bot, "user", None) else "ServerCore",
                "user": user,
                "billing": billing_summary(viewer_user_id),
            },
        )

    @app.get("/billing", response_class=HTMLResponse)
    async def billing_page(
        request: Request,
        checkout: str | None = None,
        session_id: str | None = None,
    ):
        user = session_user(request)
        if user is None:
            return RedirectResponse(url="/", status_code=302)

        viewer_user_id = session_user_id(request)
        notice = None
        notice_tone = "info"
        if checkout == "success" and session_id and viewer_user_id is not None and stripe_checkout_ready():
            try:
                stripe = load_stripe_sdk()
                checkout_session = stripe.checkout.Session.retrieve(
                    session_id,
                    expand=["subscription"],
                )
                synced = sync_checkout_session_to_billing(checkout_session, expected_user_id=viewer_user_id)
                if synced and synced.get("is_active"):
                    notice = "Premium is active for this server now. Guardian and the advanced staff toolkit are unlocked here."
                    notice_tone = "success"
                else:
                    notice = "Checkout completed, but premium is still syncing. Refresh this page in a moment if it does not appear yet."
                    notice_tone = "info"
            except Exception:
                notice = "Checkout finished, but the subscription is still syncing. Refresh in a moment if Premium does not appear yet."
                notice_tone = "info"
        elif checkout == "canceled":
            notice = "Checkout was canceled. Your server settings were not changed."
            notice_tone = "warning"

        guilds = await load_user_guilds(request)
        return render_template(
            "billing.html",
            request,
            {
                "bot_name": bot.user.name if getattr(bot, "user", None) else "ServerCore",
                "user": user,
                "billing": billing_summary(viewer_user_id),
                "comparison_rows": FREE_PREMIUM_COMPARISON_ROWS,
                "premium_price_display": PREMIUM_PRICE_DISPLAY,
                "guilds": guilds,
                "notice": notice,
                "notice_tone": notice_tone,
            },
        )

    @app.post("/billing/checkout")
    async def create_billing_checkout(request: Request):
        await require_same_origin(request)
        user = await require_user(request)
        viewer_user_id = session_user_id(request)
        if viewer_user_id is None:
            raise HTTPException(status_code=401, detail="Login required")
        if not stripe_billing_ready():
            raise HTTPException(status_code=503, detail="Stripe billing is not configured yet.")

        stripe = load_stripe_sdk()
        subscription = billing_store.get_user_subscription(viewer_user_id)
        if subscription.get("is_active") and subscription.get("stripe_customer_id"):
            portal_session = stripe.billing_portal.Session.create(
                customer=subscription["stripe_customer_id"],
                return_url=f"{dashboard_base_url}/billing",
            )
            return RedirectResponse(url=str(portal_session.url), status_code=303)

        checkout_session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": stripe_premium_price_id_value(), "quantity": 1}],
            success_url=f"{dashboard_base_url}/billing?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{dashboard_base_url}/billing?checkout=canceled",
            client_reference_id=str(viewer_user_id),
            metadata={"discord_user_id": str(viewer_user_id)},
            subscription_data={"metadata": {"discord_user_id": str(viewer_user_id)}},
            customer=subscription.get("stripe_customer_id") or None,
            allow_promotion_codes=True,
        )
        billing_store.set_checkout_session(viewer_user_id, stripe_object_id(checkout_session))
        return RedirectResponse(url=str(checkout_session.url), status_code=303)

    @app.post("/billing/portal")
    async def create_billing_portal(request: Request):
        await require_same_origin(request)
        viewer_user_id = session_user_id(request)
        if viewer_user_id is None:
            raise HTTPException(status_code=401, detail="Login required")
        if not stripe_checkout_ready():
            raise HTTPException(status_code=503, detail="Stripe billing is not configured yet.")

        subscription = billing_store.get_user_subscription(viewer_user_id)
        customer_id = subscription.get("stripe_customer_id")
        if not customer_id:
            raise HTTPException(status_code=409, detail="No Stripe customer record exists for this Discord account yet.")

        stripe = load_stripe_sdk()
        portal_session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{dashboard_base_url}/billing",
        )
        return RedirectResponse(url=str(portal_session.url), status_code=303)

    @app.post("/stripe/webhook")
    async def stripe_webhook(request: Request):
        webhook_secret = stripe_webhook_secret_value()
        if not webhook_secret:
            raise HTTPException(status_code=503, detail="Stripe webhook is not configured yet.")

        stripe = load_stripe_sdk()
        payload = await request.body()
        signature = request.headers.get("stripe-signature")
        try:
            event = stripe.Webhook.construct_event(payload, signature, webhook_secret)
        except Exception as error:
            raise HTTPException(status_code=400, detail="Invalid Stripe webhook payload.") from error

        event_id = stripe_object_id(event)
        if billing_store.has_processed_webhook(event_id):
            return JSONResponse({"received": True, "duplicate": True})

        event_type = stripe_object_value(event, "type")
        event_data = stripe_object_value(event, "data")
        event_object = None
        if isinstance(event_data, dict):
            event_object = event_data.get("object")
        else:
            event_object = getattr(event_data, "object", None)

        if event_type == "checkout.session.completed":
            session_mode = stripe_object_value(event_object, "mode")
            if session_mode == "subscription":
                sync_checkout_session_to_billing(event_object)
        elif event_type in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
            sync_subscription_event_to_billing(event_object)
        elif event_type == "invoice.paid":
            sync_invoice_event_to_billing(event_object)

        billing_store.mark_webhook_processed(event_id)
        return JSONResponse({"received": True})

    @app.get("/login")
    async def login(request: Request):
        if not oauth_ready():
            raise HTTPException(status_code=500, detail="Discord OAuth is not configured")

        oauth_state = secrets.token_urlsafe(24)
        request.session["oauth_state"] = oauth_state
        query = urlencode(
            {
                "client_id": discord_client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "identify guilds",
                "prompt": "consent",
                "state": oauth_state,
            }
        )
        return RedirectResponse(url=f"https://discord.com/oauth2/authorize?{query}", status_code=302)

    @app.get("/auth/callback")
    async def auth_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ):
        expected_state = request.session.pop("oauth_state", None)
        if error:
            clear_session(request)
            return RedirectResponse(url="/", status_code=302)
        if not code or not state or not expected_state or state != expected_state:
            clear_session(request)
            raise HTTPException(status_code=400, detail="Invalid OAuth callback")

        try:
            token_data = await fetch_discord_token(code)
            user_data = await fetch_discord_resource(token_data["access_token"], "/users/@me")
        except httpx.HTTPError as error:
            clear_session(request)
            raise HTTPException(status_code=502, detail="Discord OAuth could not be completed") from error

        dashboard_session_id = session_store.create_session(
            access_token=token_data["access_token"],
            user={
                "id": user_data["id"],
                "username": user_data["username"],
                "global_name": user_data.get("global_name"),
                "avatar": user_data.get("avatar"),
            },
        )
        request.session["dashboard_session_id"] = dashboard_session_id
        return RedirectResponse(url="/servers", status_code=302)

    @app.get("/logout")
    async def logout(request: Request):
        clear_session(request)
        return RedirectResponse(url="/", status_code=302)

    @app.get("/dashboard/{guild_id}", response_class=HTMLResponse)
    async def dashboard(request: Request, guild_id: int):
        return await render_dashboard_view(request, guild_id, "commands")

    @app.get("/dashboard/{guild_id}/setup", response_class=HTMLResponse)
    async def setup_wizard(request: Request, guild_id: int):
        return await render_setup_wizard_view(request, guild_id)

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
    async def guild_logs(
        request: Request,
        guild_id: int,
        limit: int = 80,
        query: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        category: str | None = None,
        actor: str | None = None,
    ):
        await require_guild_access(request, guild_id)
        is_premium = bot.access_manager.controls.is_premium_enabled(guild_id)
        if any(value for value in (query, kind, status, category, actor)) and not is_premium:
            raise HTTPException(status_code=402, detail="Searchable logs are part of ServerCore Premium right now.")
        safe_limit = max(1, min(limit, 500 if is_premium else 150))
        logs = premium_log_entries(
            guild_id,
            limit=safe_limit,
            query=query if is_premium else None,
            kind=kind if is_premium else None,
            status=status if is_premium else None,
            category=category if is_premium else None,
            actor=actor if is_premium else None,
        )
        return JSONResponse({"entries": logs})

    @app.get("/api/guilds/{guild_id}/logs/export")
    async def export_guild_logs(
        request: Request,
        guild_id: int,
        query: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        category: str | None = None,
        actor: str | None = None,
    ):
        await require_guild_access(request, guild_id)
        if not bot.access_manager.controls.is_premium_enabled(guild_id):
            raise HTTPException(status_code=402, detail="Log exports are part of ServerCore Premium right now.")
        return JSONResponse(
            {
                "guild_id": guild_id,
                "exported_at": time.time(),
                "entries": premium_log_entries(
                    guild_id,
                    limit=1000,
                    query=query,
                    kind=kind,
                    status=status,
                    category=category,
                    actor=actor,
                ),
            }
        )

    @app.get("/api/guilds/{guild_id}/analytics")
    async def guild_analytics(request: Request, guild_id: int):
        await require_guild_access(request, guild_id)
        if not bot.access_manager.controls.is_premium_enabled(guild_id):
            raise HTTPException(status_code=402, detail="Analytics are part of ServerCore Premium right now.")
        return JSONResponse(analytics_summary(guild_id))

    @app.get("/api/guilds/{guild_id}/cases")
    async def guild_cases(
        request: Request,
        guild_id: int,
        limit: int = 80,
        query: str | None = None,
        action: str | None = None,
    ):
        await require_guild_access(request, guild_id)
        if not bot.access_manager.controls.is_premium_enabled(guild_id):
            raise HTTPException(status_code=402, detail="Advanced case history is part of ServerCore Premium right now.")
        return JSONResponse({"entries": premium_case_entries(guild_id, limit=limit, query=query, action=action)})

    @app.get("/api/guilds/{guild_id}/cases/export")
    async def export_guild_cases(
        request: Request,
        guild_id: int,
        query: str | None = None,
        action: str | None = None,
    ):
        await require_guild_access(request, guild_id)
        if not bot.access_manager.controls.is_premium_enabled(guild_id):
            raise HTTPException(status_code=402, detail="Case exports are part of ServerCore Premium right now.")
        return JSONResponse(
            {
                "guild_id": guild_id,
                "exported_at": time.time(),
                "entries": premium_case_entries(guild_id, limit=500, query=query, action=action),
            }
        )

    @app.get("/api/guilds/{guild_id}/config-export")
    async def export_config_snapshot(request: Request, guild_id: int):
        await require_guild_access(request, guild_id)
        if not bot.access_manager.controls.is_premium_enabled(guild_id):
            raise HTTPException(status_code=402, detail="Config export is part of ServerCore Premium right now.")
        return JSONResponse(build_config_snapshot(guild_id))

    @app.post("/api/guilds/{guild_id}/config-import")
    async def import_config_snapshot(request: Request, guild_id: int, payload: ConfigImportPayload):
        await require_same_origin(request)
        await require_guild_access(request, guild_id)
        if not bot.access_manager.controls.is_premium_enabled(guild_id):
            raise HTTPException(status_code=402, detail="Config import is part of ServerCore Premium right now.")
        await apply_config_snapshot(guild_id, payload.snapshot)
        await log_dashboard_event(
            request,
            guild_id,
            title="Dashboard Config Imported",
            description="Imported a saved dashboard snapshot for this server.",
        )
        return JSONResponse({"ok": True, "subscription": subscription_summary(guild_id, session_user_id(request))})

    @app.post("/api/guilds/{guild_id}/config-template")
    async def save_config_template(request: Request, guild_id: int, payload: ConfigTemplatePayload):
        await require_same_origin(request)
        await require_guild_access(request, guild_id)
        if not bot.access_manager.controls.is_premium_enabled(guild_id):
            raise HTTPException(status_code=402, detail="Saved templates are part of ServerCore Premium right now.")
        templates = bot.access_manager.controls.save_config_template(guild_id, payload.name, build_config_snapshot(guild_id))
        await log_dashboard_event(
            request,
            guild_id,
            title="Dashboard Config Template Saved",
            description=f"Saved the current server setup as template '{payload.name}'.",
        )
        return JSONResponse({"templates": templates})

    @app.post("/api/guilds/{guild_id}/command-policy")
    async def update_command_policy(request: Request, guild_id: int, payload: CommandPolicyPayload):
        await require_same_origin(request)
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
                (
                    "Allowed Roles",
                    (
                        ", ".join(role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in policy["allowed_role_ids"])
                        or "No roles selected"
                    )
                    if policy.get("restrict_to_roles")
                    else "Discord native checks only",
                    False,
                ),
            ],
        )
        return JSONResponse(
            {
                "command_name": payload.command_name,
                "policy": policy,
                "allowed_role_names": [role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in policy["allowed_role_ids"]],
                "restrict_to_roles": bool(policy.get("restrict_to_roles")),
            }
        )

    @app.get("/api/guilds/{guild_id}/defense-state")
    async def get_defense_state(request: Request, guild_id: int):
        await require_guild_access(request, guild_id)
        return JSONResponse(defense_dashboard_summary(guild_id))

    @app.post("/api/guilds/{guild_id}/defense-state")
    async def update_defense_state(request: Request, guild_id: int, payload: DefenseTogglePayload):
        await require_same_origin(request)
        await require_guild_access(request, guild_id)
        manager = getattr(bot, "server_defense", None)
        if manager is None or not hasattr(manager, "set_defense"):
            raise HTTPException(status_code=503, detail="ServerDefense is not available")

        if payload.defense_name not in {"linkblock", "inviteblock", "antispam", "antijoin", "mentionguard", "autofilter", "lockdown", "antiraid"}:
            raise HTTPException(status_code=404, detail="Unknown defense")

        try:
            result = await run_on_bot_loop(
                manager.set_defense(
                    guild_id,
                    payload.defense_name,
                    enabled=payload.enabled,
                    duration_minutes=payload.duration_minutes,
                )
            )
        except PermissionError as error:
            raise HTTPException(status_code=402, detail=str(error)) from error
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
        await require_same_origin(request)
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

        updated = manager.update_autofilter_terms(guild_id, cleaned_terms)
        state = manager.build_dashboard_state(
            guild_id,
            {role["id"]: role["name"] for role in guild_roles(guild_id)},
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
        await require_same_origin(request)
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

    @app.get("/api/guilds/{guild_id}/subscription")
    async def get_subscription(request: Request, guild_id: int):
        await require_guild_access(request, guild_id)
        return JSONResponse(subscription_summary(guild_id, session_user_id(request)))

    @app.post("/api/guilds/{guild_id}/subscription")
    async def update_subscription(request: Request, guild_id: int, payload: SubscriptionTierPayload):
        await require_same_origin(request)
        await require_guild_access(request, guild_id)
        viewer_user_id = session_user_id(request)
        requested_tier = str(payload.tier or "").strip().lower()
        if requested_tier == TIER_PREMIUM and stripe_billing_ready():
            if viewer_user_id is None:
                raise HTTPException(status_code=401, detail="Login required")
            viewer_billing = billing_store.get_user_subscription(viewer_user_id)
            if not viewer_billing["is_active"]:
                return JSONResponse(
                    status_code=402,
                    content={
                        "detail": "Premium billing is not active on this Discord account yet.",
                        "billing_url": "/billing",
                    },
                )
            billing_store.assign_guild(guild_id, viewer_user_id)
        elif requested_tier != TIER_PREMIUM and stripe_billing_ready():
            billing_store.unassign_guild(guild_id)

        tier = bot.access_manager.controls.set_subscription_tier(guild_id, payload.tier)
        if tier != TIER_PREMIUM and hasattr(bot, "server_defense"):
            for feature_name, reason in (
                ("antiraid", "Guardian requires Premium."),
                ("lockdown", "Lockdown requires Premium."),
            ):
                try:
                    await run_on_bot_loop(bot.server_defense.disable_feature(guild_id, feature_name, reason=reason))
                except Exception:
                    if hasattr(bot.server_defense, "store"):
                        bot.server_defense.store.patch_feature(guild_id, feature_name, enabled=False, ends_at=None)
                    if feature_name == "antiraid" and hasattr(bot.server_defense, "reset_threat_state"):
                        bot.server_defense.reset_threat_state(guild_id)
        await log_dashboard_event(
            request,
            guild_id,
            title="Dashboard Subscription Tier Updated",
            description=f"Switched this server to the {tier_label(tier)} tier.",
            fields=[("Tier", tier_label(tier), True)],
        )
        session_store.clear_cached_guilds(session_id(request))
        return JSONResponse(subscription_summary(guild_id, viewer_user_id))

    @app.post("/api/guilds/{guild_id}/dashboard-access")
    async def update_dashboard_access(request: Request, guild_id: int, payload: DashboardAccessPayload):
        await require_same_origin(request)
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
        await require_same_origin(request)
        await require_guild_access(request, guild_id)

        manager = getattr(bot, "greetings", None)
        if manager is None:
            raise HTTPException(status_code=503, detail="Welcome / Leave is not available")

        flow = payload.flow.strip().lower()
        if flow not in {"welcome", "leave", "join_dm"}:
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
        elif flow == "join_dm":
            current_state = greetings_dashboard_summary(guild_id)
            join_dm_enabled = current_state.get("join_dm", {}).get("enabled", False) if payload.enabled is None else bool(payload.enabled)
            manager.set_join_dm(guild_id, enabled=join_dm_enabled, message=message)
        else:
            manager.set_leave(guild_id, channel_id=channel_id, message=message)

        await log_dashboard_event(
            request,
            guild_id,
            title="Dashboard Greeting Updated",
            description=f"Updated the {flow} flow from the dashboard.",
            fields=[
                ("Channel", next((channel["label"] for channel in guild_text_channels(guild_id) if channel["id"] == channel_id), "Direct message" if flow == "join_dm" else "Disabled"), False),
                ("Enabled", "Yes" if (bool(payload.enabled) if flow == "join_dm" else bool(channel_id)) else "No", True),
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
        await require_same_origin(request)
        await require_guild_access(request, guild_id)

        ticket_store = getattr(bot, "ticket_store", None)
        if ticket_store is None:
            raise HTTPException(status_code=503, detail="Support settings are not available")

        valid_channel_ids = {channel["id"] for channel in guild_text_channels(guild_id)}
        try:
            requested_channel_id = int(payload.command_channel_id) if payload.command_channel_id not in (None, "") else None
        except (TypeError, ValueError):
            requested_channel_id = None
        command_channel_id = requested_channel_id if requested_channel_id in valid_channel_ids else None

        cleaned_issue_types = [
            str(issue_type).strip()
            for issue_type in payload.issue_types
            if str(issue_type).strip()
        ]
        updated_issue_types = ticket_store.set_issue_types(guild_id, cleaned_issue_types)
        ticket_store.set_support_command_channel_id(guild_id, command_channel_id)
        await log_dashboard_event(
            request,
            guild_id,
            title="Dashboard Support Intake Updated",
            description="Updated the support issue list used by /ticket.",
            fields=[
                ("Issue Types", ", ".join(updated_issue_types), False),
                ("Ticket Command Channel", next((channel["label"] for channel in guild_text_channels(guild_id) if channel["id"] == command_channel_id), "Any channel"), False),
            ],
        )
        return JSONResponse(support_dashboard_summary(guild_id))

    @app.get("/api/guilds/{guild_id}/purge-settings")
    async def get_purge_settings(request: Request, guild_id: int):
        await require_guild_access(request, guild_id)
        return JSONResponse(purge_settings_summary(guild_id))

    @app.post("/api/guilds/{guild_id}/purge-settings")
    async def update_purge_settings(request: Request, guild_id: int, payload: PurgeSettingsPayload):
        await require_same_origin(request)
        await require_guild_access(request, guild_id)
        controls = bot.access_manager.controls
        allowed_limit = min(int(payload.limit), controls.FREE_PURGE_LIMIT_CAP)
        updated = controls.set_purge_settings(
            guild_id,
            limit=allowed_limit,
            default_mode=payload.default_mode,
            include_pinned_default=payload.include_pinned_default,
        )
        await log_dashboard_event(
            request,
            guild_id,
            title="Dashboard Purge Limit Updated",
            description="Updated the maximum cleanup size from the dashboard.",
            fields=[
                ("Purge Limit", str(min(updated["limit"], controls.FREE_PURGE_LIMIT_CAP)), True),
                ("Default Mode", updated["default_mode"], True),
                ("Pinned Messages", "Included" if updated["include_pinned_default"] else "Protected", True),
            ],
        )
        return JSONResponse(purge_settings_summary(guild_id))

    @app.get("/api/guilds/{guild_id}/moderation-settings")
    async def get_moderation_settings(request: Request, guild_id: int):
        await require_guild_access(request, guild_id)
        return JSONResponse(moderation_settings_summary(guild_id))

    @app.post("/api/guilds/{guild_id}/moderation-settings")
    async def update_moderation_settings(request: Request, guild_id: int, payload: ModerationSettingsPayload):
        await require_same_origin(request)
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

    @app.get("/api/guilds/{guild_id}/alert-settings")
    async def get_alert_settings(request: Request, guild_id: int):
        await require_guild_access(request, guild_id)
        return JSONResponse(alert_settings_summary(guild_id))

    @app.post("/api/guilds/{guild_id}/alert-settings")
    async def update_alert_settings(request: Request, guild_id: int, payload: AlertSettingsPayload):
        await require_same_origin(request)
        await require_guild_access(request, guild_id)
        updated = bot.access_manager.controls.set_alert_settings(
            guild_id,
            confirmation_enabled=payload.confirmation_enabled,
            skip_in_voice_default=payload.skip_in_voice_default,
            only_offline_default=payload.only_offline_default,
            include_bots_default=payload.include_bots_default,
            cooldown_seconds=payload.cooldown_seconds,
        )
        await log_dashboard_event(
            request,
            guild_id,
            title="Dashboard Role Alerts Updated",
            description="Updated the default delivery rules for role alerts.",
            fields=[
                ("Confirmation", "On" if updated["confirmation_enabled"] else "Off", True),
                ("Skip In Voice", "Yes" if updated["skip_in_voice_default"] else "No", True),
                ("Only Offline", "Yes" if updated["only_offline_default"] else "No", True),
                ("Include Bots", "Yes" if updated["include_bots_default"] else "No", True),
                ("Cooldown", f"{updated['cooldown_seconds']} seconds", True),
            ],
        )
        return JSONResponse(updated)

    @app.post("/api/guilds/{guild_id}/setup-wizard")
    async def update_setup_wizard(request: Request, guild_id: int, payload: SetupWizardPayload):
        await require_same_origin(request)
        await require_guild_access(request, guild_id)

        valid_role_ids = {role["id"] for role in guild_roles(guild_id)}
        valid_channel_ids = {channel["id"] for channel in guild_text_channels(guild_id)}

        def clean_role_ids(values: list[int]) -> list[int]:
            return [role_id for role_id in values if role_id in valid_role_ids]

        def clean_channel_id(value: int | str | None) -> int | None:
            if value in (None, "", False):
                return None
            try:
                channel_id = int(value)
            except (TypeError, ValueError):
                return None
            return channel_id if channel_id in valid_channel_ids else None

        moderation_role_ids = clean_role_ids(payload.moderation_role_ids)
        support_role_ids = clean_role_ids(payload.support_role_ids)
        community_role_ids = clean_role_ids(payload.community_role_ids)
        autorole_role_ids = clean_role_ids(payload.autorole_role_ids)

        bot.access_manager.controls.set_moderation_settings(
            guild_id,
            confirmation_enabled=payload.moderation_confirmation_enabled,
            default_timeout_minutes=payload.default_timeout_minutes,
        )
        bot.access_manager.controls.set_autorole_role_ids(guild_id, autorole_role_ids)

        for command_name in setup_command_groups["moderation"]:
            bot.access_manager.controls.set_roles(
                guild_id,
                command_name,
                moderation_role_ids,
                restrict_to_roles=not payload.moderation_allow_everyone,
            )
        for command_name in setup_command_groups["support"]:
            bot.access_manager.controls.set_roles(
                guild_id,
                command_name,
                support_role_ids,
                restrict_to_roles=not payload.support_allow_everyone,
            )
        for command_name in setup_command_groups["community"]:
            bot.access_manager.controls.set_roles(
                guild_id,
                command_name,
                community_role_ids,
                restrict_to_roles=not payload.community_allow_everyone,
            )

        manager = getattr(bot, "greetings", None)
        if manager is not None:
            manager.set_welcome(
                guild_id,
                channel_id=clean_channel_id(payload.welcome_channel_id),
                message=payload.welcome_message,
            )
            manager.set_leave(
                guild_id,
                channel_id=clean_channel_id(payload.leave_channel_id),
                message=payload.leave_message,
            )
            manager.set_join_dm(
                guild_id,
                enabled=payload.join_dm_enabled,
                message=payload.join_dm_message,
            )

        ticket_store = getattr(bot, "ticket_store", None)
        if ticket_store is not None:
            cleaned_issue_types = [
                str(issue_type).strip()
                for issue_type in payload.support_issue_types
                if str(issue_type).strip()
            ]
            if cleaned_issue_types:
                ticket_store.set_issue_types(guild_id, cleaned_issue_types)
            ticket_store.set_support_command_channel_id(guild_id, clean_channel_id(payload.support_command_channel_id))

        bot.access_manager.controls.set_setup_wizard_completed(guild_id, True)

        role_lookup = {role["id"]: role["name"] for role in guild_roles(guild_id)}
        await log_dashboard_event(
            request,
            guild_id,
            title="Setup Wizard Completed",
            description="Saved the server's guided setup settings from the dashboard.",
            fields=[
                (
                    "Moderation Roles",
                    "Allow everyone"
                    if payload.moderation_allow_everyone
                    else (", ".join(role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in moderation_role_ids) or "No roles selected"),
                    False,
                ),
                (
                    "Support Roles",
                    "Allow everyone"
                    if payload.support_allow_everyone
                    else (", ".join(role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in support_role_ids) or "No roles selected"),
                    False,
                ),
                ("Support Command Channel", next((channel["label"] for channel in guild_text_channels(guild_id) if channel["id"] == clean_channel_id(payload.support_command_channel_id)), "Any channel"), False),
                (
                    "Community Roles",
                    "Allow everyone"
                    if payload.community_allow_everyone
                    else (", ".join(role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in community_role_ids) or "No roles selected"),
                    False,
                ),
                ("Autoroles", ", ".join(role_lookup.get(role_id, f"Deleted ({role_id})") for role_id in autorole_role_ids) or "None", False),
            ],
        )
        return JSONResponse(
            {
                "completed": True,
                "redirect_url": f"/dashboard/{guild_id}",
                "setup_summary": setup_wizard_summary(guild_id),
                "greetings_summary": greetings_dashboard_summary(guild_id),
                "moderation_settings": moderation_settings_summary(guild_id),
                "support_summary": support_dashboard_summary(guild_id),
            }
        )

    return app


class DashboardServer:
    def __init__(self, bot, command_controls, command_logs):
        self.bot = bot
        self.command_controls = command_controls
        self.command_logs = command_logs
        self._thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None

    async def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._server, self._thread = start_dashboard_server(self.bot)

    async def stop(self):
        if self._server is not None:
            self._server.should_exit = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        self._server = None
        self._thread = None


def start_dashboard_server(bot) -> tuple[uvicorn.Server, threading.Thread]:
    port = resolve_dashboard_port()
    host = resolve_dashboard_host()
    app = create_dashboard_app(bot)
    config = uvicorn.Config(app=app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config=config)

    thread = threading.Thread(target=server.run, daemon=True, name="servercore-dashboard")
    thread.start()
    return server, thread
