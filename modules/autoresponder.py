import json
import time
from pathlib import Path

import discord
from discord.ext import commands
from discord import app_commands

DATA_FILE = Path("autoresponders.json")
MAX_AUTORESPONDERS_PER_GUILD = 3
DEFAULT_COOLDOWN_SECONDS = 25


def load_data() -> dict:
    if not DATA_FILE.exists():
        return {}

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


class AutoResponder(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = load_data()
        self.cooldowns = {}  # (guild_id, trigger) -> last_used_time

    # ----------------------------
    # DATA HELPERS
    # ----------------------------
    def ensure_guild_entry(self, guild_id: int) -> None:
        guild_id_str = str(guild_id)
        if guild_id_str not in self.data:
            self.data[guild_id_str] = {
                "enabled": True,
                "responders": []
            }

    def get_guild_data(self, guild_id: int) -> dict:
        self.ensure_guild_entry(guild_id)
        return self.data[str(guild_id)]

    def get_responders(self, guild_id: int) -> list:
        return self.get_guild_data(guild_id)["responders"]

    def is_enabled(self, guild_id: int) -> bool:
        return self.get_guild_data(guild_id).get("enabled", True)

    def set_enabled(self, guild_id: int, enabled: bool) -> None:
        guild_data = self.get_guild_data(guild_id)
        guild_data["enabled"] = enabled
        save_data(self.data)

    def save(self) -> None:
        save_data(self.data)

    def find_responder(self, guild_id: int, trigger: str):
        trigger = trigger.lower().strip()
        for responder in self.get_responders(guild_id):
            if responder["trigger"].lower() == trigger:
                return responder
        return None

    def can_fire_cooldown(self, guild_id: int, trigger: str) -> bool:
        key = (guild_id, trigger.lower())
        now = time.time()
        last_used = self.cooldowns.get(key, 0)

        if now - last_used < DEFAULT_COOLDOWN_SECONDS:
            return False

        self.cooldowns[key] = now
        return True

    def user_can_manage(self, interaction: discord.Interaction) -> bool:
        return (
            interaction.guild is not None
            and isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.manage_guild
        )

    # ----------------------------
    # COMMAND GROUP
    # ----------------------------
    autoresponder = app_commands.Group(
        name="autoresponder",
        description="Set up automatic replies for common messages"
    )

    @autoresponder.command(name="add", description="Add a new autoresponder")
    @app_commands.describe(
        trigger="What message should activate the response",
        response="What the bot should say back",
        match_type="How the trigger should match messages"
    )
    @app_commands.choices(match_type=[
        app_commands.Choice(name="exact", value="exact"),
        app_commands.Choice(name="contains", value="contains"),
    ])
    async def add(
        self,
        interaction: discord.Interaction,
        trigger: app_commands.Range[str, 1, 80],
        response: app_commands.Range[str, 1, 500],
        match_type: app_commands.Choice[str]
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.",
                ephemeral=True
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ I couldn't verify your server permissions.",
                ephemeral=True
            )
            return

        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ You need **Manage Server** to use autoresponders.",
                ephemeral=True
            )
            return

        responders = self.get_responders(interaction.guild.id)

        if len(responders) >= MAX_AUTORESPONDERS_PER_GUILD:
            await interaction.response.send_message(
                f"❌ Free tier allows up to **{MAX_AUTORESPONDERS_PER_GUILD}** autoresponders per server.",
                ephemeral=True
            )
            return

        trigger = trigger.strip()
        response = response.strip()

        if self.find_responder(interaction.guild.id, trigger):
            await interaction.response.send_message(
                "❌ That trigger already exists. Use a different one or edit the old one.",
                ephemeral=True
            )
            return

        responders.append({
            "trigger": trigger,
            "response": response,
            "match_type": match_type.value,
            "created_by": interaction.user.id
        })
        self.save()

        embed = discord.Embed(
            title="✅ Autoresponder Added",
            color=discord.Color.green()
        )
        embed.add_field(name="Trigger", value=trigger, inline=False)
        embed.add_field(name="Response", value=response, inline=False)
        embed.add_field(name="Match Type", value=match_type.value.title(), inline=False)
        embed.set_footer(text=f"{len(responders)}/{MAX_AUTORESPONDERS_PER_GUILD} free-tier autoresponders used")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @autoresponder.command(name="edit", description="Edit an existing autoresponder")
    @app_commands.describe(
        trigger="The current trigger you want to edit",
        new_trigger="Optional new trigger text",
        new_response="Optional new response text",
        new_match_type="Optional new match type"
    )
    @app_commands.choices(new_match_type=[
        app_commands.Choice(name="exact", value="exact"),
        app_commands.Choice(name="contains", value="contains"),
    ])
    async def edit(
        self,
        interaction: discord.Interaction,
        trigger: app_commands.Range[str, 1, 80],
        new_trigger: str | None = None,
        new_response: str | None = None,
        new_match_type: app_commands.Choice[str] | None = None
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.",
                ephemeral=True
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ I couldn't verify your server permissions.",
                ephemeral=True
            )
            return

        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ You need **Manage Server** to use autoresponders.",
                ephemeral=True
            )
            return

        responder = self.find_responder(interaction.guild.id, trigger)
        if responder is None:
            await interaction.response.send_message(
                "❌ I couldn't find an autoresponder with that trigger.",
                ephemeral=True
            )
            return

        if new_trigger is None and new_response is None and new_match_type is None:
            await interaction.response.send_message(
                "❌ You need to change at least one thing: trigger, response, or match type.",
                ephemeral=True
            )
            return

        old_trigger = responder["trigger"]
        old_response = responder["response"]
        old_match_type = responder.get("match_type", "exact")

        if new_trigger is not None:
            new_trigger = new_trigger.strip()
            existing = self.find_responder(interaction.guild.id, new_trigger)
            if existing is not None and existing is not responder:
                await interaction.response.send_message(
                    "❌ Another autoresponder already uses that new trigger.",
                    ephemeral=True
                )
                return
            responder["trigger"] = new_trigger

        if new_response is not None:
            responder["response"] = new_response.strip()

        if new_match_type is not None:
            responder["match_type"] = new_match_type.value

        self.save()

        embed = discord.Embed(
            title="✏️ Autoresponder Updated",
            color=discord.Color.blurple()
        )
        embed.add_field(
            name="Before",
            value=(
                f"**Trigger:** `{old_trigger}`\n"
                f"**Response:** {old_response}\n"
                f"**Type:** `{old_match_type}`"
            ),
            inline=False
        )
        embed.add_field(
            name="After",
            value=(
                f"**Trigger:** `{responder['trigger']}`\n"
                f"**Response:** {responder['response']}\n"
                f"**Type:** `{responder.get('match_type', 'exact')}`"
            ),
            inline=False
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @autoresponder.command(name="remove", description="Remove an autoresponder")
    @app_commands.describe(trigger="The trigger you want to remove")
    async def remove(self, interaction: discord.Interaction, trigger: str):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.",
                ephemeral=True
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ I couldn't verify your server permissions.",
                ephemeral=True
            )
            return

        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ You need **Manage Server** to use autoresponders.",
                ephemeral=True
            )
            return

        responders = self.get_responders(interaction.guild.id)
        trigger_clean = trigger.lower().strip()

        responder_to_remove = None
        for responder in responders:
            if responder["trigger"].lower() == trigger_clean:
                responder_to_remove = responder
                break

        if responder_to_remove is None:
            await interaction.response.send_message(
                "❌ I couldn't find an autoresponder with that trigger.",
                ephemeral=True
            )
            return

        responders.remove(responder_to_remove)
        self.save()

        await interaction.response.send_message(
            f"🗑️ Removed autoresponder for trigger `{responder_to_remove['trigger']}`.",
            ephemeral=True
        )

    @autoresponder.command(name="list", description="View all autoresponders in this server")
    async def list(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.",
                ephemeral=True
            )
            return

        responders = self.get_responders(interaction.guild.id)

        if not responders:
            await interaction.response.send_message(
                "This server does not have any autoresponders yet.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🤖 Autoresponders",
            description="Here are the automatic replies set up for this server.",
            color=discord.Color.blurple()
        )

        lines = []
        for responder in responders[:10]:
            lines.append(
                f"**Trigger:** `{responder['trigger']}`\n"
                f"**Type:** `{responder['match_type']}`\n"
                f"**Reply:** {responder['response']}"
            )

        embed.description = "\n\n".join(lines)

        if len(responders) > 10:
            embed.set_footer(text=f"Showing 10 of {len(responders)} autoresponders")
        else:
            embed.set_footer(text=f"{len(responders)}/{MAX_AUTORESPONDERS_PER_GUILD} free-tier autoresponders used")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @autoresponder.command(name="toggle", description="Turn autoresponders on or off for this server")
    @app_commands.describe(enabled="Whether autoresponders should be enabled")
    async def toggle(self, interaction: discord.Interaction, enabled: bool):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.",
                ephemeral=True
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ I couldn't verify your server permissions.",
                ephemeral=True
            )
            return

        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ You need **Manage Server** to use autoresponders.",
                ephemeral=True
            )
            return

        self.set_enabled(interaction.guild.id, enabled)

        await interaction.response.send_message(
            f"✅ Autoresponders are now **{'enabled' if enabled else 'disabled'}** for this server.",
            ephemeral=True
        )

    # ----------------------------
    # MESSAGE LISTENER
    # ----------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return

        if message.author.bot:
            return

        if not self.is_enabled(message.guild.id):
            return

        content = message.content.strip()
        if not content:
            return

        responders = self.get_responders(message.guild.id)
        if not responders:
            return

        content_lower = content.lower()

        for responder in responders:
            trigger = responder["trigger"].lower()
            match_type = responder.get("match_type", "exact")

            matched = False

            if match_type == "exact":
                matched = content_lower == trigger
            elif match_type == "contains":
                matched = trigger in content_lower

            if not matched:
                continue

            if not self.can_fire_cooldown(message.guild.id, responder["trigger"]):
                return

            try:
                await message.channel.send(responder["response"])
            except discord.HTTPException:
                pass

            return


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoResponder(bot))