from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from core.premium import command_limit, guild_has_premium, limit_reached_message, usage_footer
from core.storage import read_json, write_json

DATA_FILE = Path("customcommands.json")
FREE_CUSTOM_COMMAND_LIMIT = 3


def load_data() -> dict:
    data = read_json(DATA_FILE, {})
    return data if isinstance(data, dict) else {}


def save_data(data: dict) -> None:
    write_json(DATA_FILE, data)


class CustomCommands(commands.Cog):
    customcommand = app_commands.Group(
        name="customcommand",
        description="Create and manage simple custom prefix commands for your server",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = load_data()

    def _refresh(self) -> None:
        self.data = load_data()

    def _save(self) -> None:
        save_data(self.data)

    def ensure_guild_entry(self, guild_id: int) -> None:
        guild_key = str(guild_id)
        if guild_key not in self.data:
            self.data[guild_key] = {"commands": {}}

    def get_guild_commands(self, guild_id: int) -> dict:
        self._refresh()
        self.ensure_guild_entry(guild_id)
        return self.data[str(guild_id)]["commands"]

    def normalize_name(self, name: str) -> str:
        return name.strip().lower()

    def _premium_enabled(self, guild_id: int, interaction: discord.Interaction | None = None) -> bool:
        return guild_has_premium(self.bot, guild_id, interaction)

    def _limit(self, guild_id: int, interaction: discord.Interaction | None = None) -> int | None:
        return command_limit(FREE_CUSTOM_COMMAND_LIMIT, premium_enabled=self._premium_enabled(guild_id, interaction))

    def _footer(self, guild_id: int, item_count: int, interaction: discord.Interaction | None = None) -> str:
        return usage_footer(
            item_count,
            "custom commands",
            FREE_CUSTOM_COMMAND_LIMIT,
            premium_enabled=self._premium_enabled(guild_id, interaction),
        )

    async def _require_manager(self, interaction: discord.Interaction) -> discord.Member | None:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return None
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("I couldn't verify your server permissions.", ephemeral=True)
            return None
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You need Manage Server to manage custom commands.",
                ephemeral=True,
            )
            return None
        return interaction.user

    def is_reserved_command(self, name: str) -> bool:
        normalized = self.normalize_name(name)
        reserved = {
            "help",
            "poll",
            "pollresults",
            "endpoll",
            "lock",
            "unlock",
            "slowmode",
            "afk",
            "avatar",
            "banner",
            "userinfo",
            "serverstats",
            "membercount",
            "remind",
            "myreminders",
            "removereminder",
            "setwelcome",
            "setauditlog",
            "removeauditlog",
            "giveauditrole",
            "reactionrole",
            "autoresponder",
            "customcommand",
            "dashboard",
            "purge",
            "color",
            "wiki",
            "ticket",
        }
        return normalized in reserved

    @customcommand.command(name="add", description="Save a new prefix command")
    @app_commands.describe(
        name="The command name members will type after !",
        response="What the bot should send back",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 32],
        response: app_commands.Range[str, 1, 500],
    ) -> None:
        manager = await self._require_manager(interaction)
        if manager is None or interaction.guild is None:
            return

        normalized_name = self.normalize_name(name)
        guild_commands = self.get_guild_commands(interaction.guild.id)
        limit = self._limit(interaction.guild.id, interaction)

        if not normalized_name.isalnum():
            await interaction.response.send_message(
                "Command names can only use letters and numbers.",
                ephemeral=True,
            )
            return
        if self.is_reserved_command(normalized_name):
            await interaction.response.send_message(
                "That name is already used by ServerCore. Pick a different custom command name.",
                ephemeral=True,
            )
            return
        if normalized_name in guild_commands:
            await interaction.response.send_message(
                "That custom command already exists in this server.",
                ephemeral=True,
            )
            return
        if limit is not None and len(guild_commands) >= limit:
            await interaction.response.send_message(
                limit_reached_message("custom commands in this server", limit),
                ephemeral=True,
            )
            return

        guild_commands[normalized_name] = {
            "response": response.strip(),
            "created_by": interaction.user.id,
        }
        self._save()

        embed = discord.Embed(
            title="Custom command saved",
            description="Members can now use this command with the server prefix.",
            color=discord.Color.green(),
        )
        embed.add_field(name="Command", value=f"`!{normalized_name}`", inline=False)
        embed.add_field(name="Reply", value=response.strip(), inline=False)
        embed.set_footer(text=self._footer(interaction.guild.id, len(guild_commands), interaction))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @customcommand.command(name="edit", description="Update one of your saved custom commands")
    @app_commands.describe(
        name="The current custom command name",
        new_name="Optional new name",
        new_response="Optional new reply",
    )
    async def edit(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 32],
        new_name: app_commands.Range[str, 1, 32] | None = None,
        new_response: app_commands.Range[str, 1, 500] | None = None,
    ) -> None:
        manager = await self._require_manager(interaction)
        if manager is None or interaction.guild is None:
            return

        guild_commands = self.get_guild_commands(interaction.guild.id)
        current_name = self.normalize_name(name)
        if current_name not in guild_commands:
            await interaction.response.send_message(
                "That custom command does not exist in this server.",
                ephemeral=True,
            )
            return
        if new_name is None and new_response is None:
            await interaction.response.send_message(
                "Choose a new name, a new reply, or both.",
                ephemeral=True,
            )
            return

        updated_name = current_name
        if new_name is not None:
            updated_name = self.normalize_name(new_name)
            if not updated_name.isalnum():
                await interaction.response.send_message(
                    "Command names can only use letters and numbers.",
                    ephemeral=True,
                )
                return
            if self.is_reserved_command(updated_name):
                await interaction.response.send_message(
                    "That name is already used by ServerCore. Pick a different custom command name.",
                    ephemeral=True,
                )
                return
            if updated_name != current_name and updated_name in guild_commands:
                await interaction.response.send_message(
                    "Another custom command already uses that name.",
                    ephemeral=True,
                )
                return

        current = guild_commands[current_name]
        new_payload = {
            "response": new_response.strip() if new_response is not None else current["response"],
            "created_by": current.get("created_by", interaction.user.id),
        }
        if updated_name != current_name:
            del guild_commands[current_name]
        guild_commands[updated_name] = new_payload
        self._save()

        embed = discord.Embed(
            title="Custom command updated",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Command", value=f"`!{updated_name}`", inline=False)
        embed.add_field(name="Reply", value=new_payload["response"], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @customcommand.command(name="remove", description="Delete a custom command")
    @app_commands.describe(name="The custom command name to delete")
    async def remove(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 32]) -> None:
        manager = await self._require_manager(interaction)
        if manager is None or interaction.guild is None:
            return

        guild_commands = self.get_guild_commands(interaction.guild.id)
        normalized_name = self.normalize_name(name)
        if normalized_name not in guild_commands:
            await interaction.response.send_message(
                "That custom command does not exist in this server.",
                ephemeral=True,
            )
            return

        del guild_commands[normalized_name]
        self._save()
        await interaction.response.send_message(
            f"Removed `!{normalized_name}`.",
            ephemeral=True,
        )

    @customcommand.command(name="list", description="Show the custom commands saved in this server")
    async def list_commands(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        guild_commands = self.get_guild_commands(interaction.guild.id)
        if not guild_commands:
            await interaction.response.send_message(
                "This server has no custom commands yet.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Custom commands",
            description="Members can use these with the `!` prefix.",
            color=discord.Color.blurple(),
        )
        embed.description = "\n\n".join(
            f"**`!{command_name}`**\n{command_data['response']}"
            for command_name, command_data in list(guild_commands.items())[:15]
        )
        embed.set_footer(text=self._footer(interaction.guild.id, len(guild_commands)))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return

        content = message.content.strip()
        if not content.startswith("!"):
            return
        parts = content[1:].split()
        if not parts:
            return

        command_name = self.normalize_name(parts[0])
        guild_commands = self.get_guild_commands(message.guild.id)
        if command_name not in guild_commands:
            return

        response = str(guild_commands[command_name].get("response", ""))
        response = response.replace("{user}", message.author.mention)
        response = response.replace("{username}", message.author.name)
        response = response.replace("{server}", message.guild.name)
        response = response.replace("{channel}", message.channel.mention)

        try:
            await message.channel.send(response)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(CustomCommands(bot))
