from __future__ import annotations

import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from core.premium import command_limit, guild_has_premium, limit_reached_message, usage_footer
from core.storage import read_json, write_json

DATA_FILE = Path("autoresponders.json")
FREE_AUTORESPONDER_LIMIT = 3
DEFAULT_COOLDOWN_SECONDS = 25


def load_data() -> dict:
    data = read_json(DATA_FILE, {})
    return data if isinstance(data, dict) else {}


def save_data(data: dict) -> None:
    write_json(DATA_FILE, data)


class AutoResponder(commands.Cog):
    autoresponder = app_commands.Group(
        name="autoresponder",
        description="Set up automatic replies for common messages",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = load_data()
        self.cooldowns: dict[tuple[int, str], float] = {}

    def _refresh(self) -> None:
        self.data = load_data()

    def _save(self) -> None:
        save_data(self.data)

    def ensure_guild_entry(self, guild_id: int) -> None:
        guild_key = str(guild_id)
        if guild_key not in self.data:
            self.data[guild_key] = {"enabled": True, "responders": []}

    def get_guild_data(self, guild_id: int) -> dict:
        self._refresh()
        self.ensure_guild_entry(guild_id)
        return self.data[str(guild_id)]

    def get_responders(self, guild_id: int) -> list[dict]:
        return self.get_guild_data(guild_id).setdefault("responders", [])

    def is_enabled(self, guild_id: int) -> bool:
        return bool(self.get_guild_data(guild_id).get("enabled", True))

    def set_enabled(self, guild_id: int, enabled: bool) -> None:
        guild_data = self.get_guild_data(guild_id)
        guild_data["enabled"] = bool(enabled)
        self._save()

    def find_responder(self, guild_id: int, trigger: str) -> dict | None:
        desired = trigger.strip().casefold()
        for responder in self.get_responders(guild_id):
            if str(responder.get("trigger", "")).casefold() == desired:
                return responder
        return None

    def can_fire_cooldown(self, guild_id: int, trigger: str) -> bool:
        key = (guild_id, trigger.casefold())
        now = time.time()
        last_used = self.cooldowns.get(key, 0.0)
        if now - last_used < DEFAULT_COOLDOWN_SECONDS:
            return False
        self.cooldowns[key] = now
        return True

    def _premium_enabled(self, guild_id: int, interaction: discord.Interaction | None = None) -> bool:
        return guild_has_premium(self.bot, guild_id, interaction)

    async def _require_manager(self, interaction: discord.Interaction) -> discord.Member | None:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return None
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("I couldn't verify your server permissions.", ephemeral=True)
            return None
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You need Manage Server to manage autoresponders.",
                ephemeral=True,
            )
            return None
        return interaction.user

    def _limit(self, guild_id: int, interaction: discord.Interaction | None = None) -> int | None:
        return command_limit(FREE_AUTORESPONDER_LIMIT, premium_enabled=self._premium_enabled(guild_id, interaction))

    def _limit_footer(self, guild_id: int, item_count: int, interaction: discord.Interaction | None = None) -> str:
        return usage_footer(
            item_count,
            "autoresponders",
            FREE_AUTORESPONDER_LIMIT,
            premium_enabled=self._premium_enabled(guild_id, interaction),
        )

    @autoresponder.command(name="add", description="Create a new automatic reply")
    @app_commands.describe(
        trigger="The message members need to send",
        response="What the bot should reply with",
        match_type="Whether the trigger must match exactly or just be included",
    )
    @app_commands.choices(
        match_type=[
            app_commands.Choice(name="Exact match", value="exact"),
            app_commands.Choice(name="Contains text", value="contains"),
        ]
    )
    async def add(
        self,
        interaction: discord.Interaction,
        trigger: app_commands.Range[str, 1, 80],
        response: app_commands.Range[str, 1, 500],
        match_type: app_commands.Choice[str],
    ) -> None:
        manager = await self._require_manager(interaction)
        if manager is None or interaction.guild is None:
            return

        responders = self.get_responders(interaction.guild.id)
        limit = self._limit(interaction.guild.id, interaction)
        if limit is not None and len(responders) >= limit:
            await interaction.response.send_message(
                limit_reached_message("autoresponders in this server", limit),
                ephemeral=True,
            )
            return

        trigger = trigger.strip()
        response = response.strip()
        if self.find_responder(interaction.guild.id, trigger):
            await interaction.response.send_message(
                "That trigger already exists. Edit or remove the old one first.",
                ephemeral=True,
            )
            return

        responders.append(
            {
                "trigger": trigger,
                "response": response,
                "match_type": match_type.value,
                "created_by": interaction.user.id,
            }
        )
        self._save()

        embed = discord.Embed(
            title="Autoresponder saved",
            description="This automatic reply is now active for the server.",
            color=discord.Color.green(),
        )
        embed.add_field(name="Trigger", value=trigger, inline=False)
        embed.add_field(name="Reply", value=response, inline=False)
        embed.add_field(name="Match type", value=match_type.name, inline=True)
        embed.set_footer(text=self._limit_footer(interaction.guild.id, len(responders), interaction))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @autoresponder.command(name="edit", description="Update one of your saved automatic replies")
    @app_commands.describe(
        trigger="The current trigger you want to update",
        new_trigger="Optional new trigger",
        new_response="Optional new reply",
        new_match_type="Optional new match type",
    )
    @app_commands.choices(
        new_match_type=[
            app_commands.Choice(name="Exact match", value="exact"),
            app_commands.Choice(name="Contains text", value="contains"),
        ]
    )
    async def edit(
        self,
        interaction: discord.Interaction,
        trigger: app_commands.Range[str, 1, 80],
        new_trigger: app_commands.Range[str, 1, 80] | None = None,
        new_response: app_commands.Range[str, 1, 500] | None = None,
        new_match_type: app_commands.Choice[str] | None = None,
    ) -> None:
        manager = await self._require_manager(interaction)
        if manager is None or interaction.guild is None:
            return

        responder = self.find_responder(interaction.guild.id, trigger)
        if responder is None:
            await interaction.response.send_message(
                "I couldn't find an autoresponder with that trigger.",
                ephemeral=True,
            )
            return

        if new_trigger is None and new_response is None and new_match_type is None:
            await interaction.response.send_message(
                "Choose at least one thing to update: the trigger, the reply, or the match type.",
                ephemeral=True,
            )
            return

        before = {
            "trigger": responder["trigger"],
            "response": responder["response"],
            "match_type": responder.get("match_type", "exact"),
        }

        if new_trigger is not None:
            candidate = new_trigger.strip()
            existing = self.find_responder(interaction.guild.id, candidate)
            if existing is not None and existing is not responder:
                await interaction.response.send_message(
                    "Another autoresponder already uses that trigger.",
                    ephemeral=True,
                )
                return
            responder["trigger"] = candidate

        if new_response is not None:
            responder["response"] = new_response.strip()

        if new_match_type is not None:
            responder["match_type"] = new_match_type.value

        self._save()

        embed = discord.Embed(
            title="Autoresponder updated",
            description="The automatic reply has been refreshed.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Before",
            value=(
                f"Trigger: `{before['trigger']}`\n"
                f"Reply: {before['response']}\n"
                f"Match type: {before['match_type'].replace('_', ' ').title()}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Now",
            value=(
                f"Trigger: `{responder['trigger']}`\n"
                f"Reply: {responder['response']}\n"
                f"Match type: {str(responder.get('match_type', 'exact')).replace('_', ' ').title()}"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @autoresponder.command(name="remove", description="Delete an automatic reply")
    @app_commands.describe(trigger="The trigger you want to remove")
    async def remove(self, interaction: discord.Interaction, trigger: app_commands.Range[str, 1, 80]) -> None:
        manager = await self._require_manager(interaction)
        if manager is None or interaction.guild is None:
            return

        responders = self.get_responders(interaction.guild.id)
        responder = self.find_responder(interaction.guild.id, trigger)
        if responder is None:
            await interaction.response.send_message(
                "I couldn't find an autoresponder with that trigger.",
                ephemeral=True,
            )
            return

        responders.remove(responder)
        self._save()
        await interaction.response.send_message(
            f"Removed the autoresponder for `{responder['trigger']}`.",
            ephemeral=True,
        )

    @autoresponder.command(name="list", description="Show the automatic replies saved in this server")
    async def list(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        responders = self.get_responders(interaction.guild.id)
        if not responders:
            await interaction.response.send_message(
                "This server does not have any autoresponders yet.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Autoresponders",
            description="These automatic replies are currently active in this server.",
            color=discord.Color.blurple(),
        )
        embed.description = "\n\n".join(
            (
                f"Trigger: `{responder['trigger']}`\n"
                f"Match type: {str(responder.get('match_type', 'exact')).replace('_', ' ').title()}\n"
                f"Reply: {responder['response']}"
            )
            for responder in responders[:10]
        )
        embed.set_footer(text=self._limit_footer(interaction.guild.id, len(responders)))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @autoresponder.command(name="toggle", description="Turn autoresponders on or off for this server")
    @app_commands.describe(enabled="Choose whether autoresponders should be on")
    async def toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        manager = await self._require_manager(interaction)
        if manager is None or interaction.guild is None:
            return

        self.set_enabled(interaction.guild.id, enabled)
        await interaction.response.send_message(
            "Autoresponders are now on for this server." if enabled else "Autoresponders are now off for this server.",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not self.is_enabled(message.guild.id):
            return

        content = message.content.strip()
        if not content:
            return

        responders = self.get_responders(message.guild.id)
        if not responders:
            return

        content_lower = content.casefold()
        for responder in responders:
            trigger = str(responder.get("trigger", "")).casefold()
            match_type = str(responder.get("match_type", "exact")).casefold()

            matched = content_lower == trigger if match_type == "exact" else trigger in content_lower
            if not matched:
                continue

            if not self.can_fire_cooldown(message.guild.id, str(responder.get("trigger", ""))):
                return

            try:
                await message.channel.send(str(responder.get("response", "")))
            except discord.HTTPException:
                pass
            return


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoResponder(bot))
