import json
from pathlib import Path

import discord
from discord.ext import commands
from discord import app_commands

DATA_FILE = Path("customcommands.json")
MAX_CUSTOM_COMMANDS_PER_GUILD = 3  # free tier limit


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


class CustomCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = load_data()

    # ----------------------------
    # DATA HELPERS
    # ----------------------------
    def ensure_guild_entry(self, guild_id: int) -> None:
        guild_id_str = str(guild_id)
        if guild_id_str not in self.data:
            self.data[guild_id_str] = {"commands": {}}

    def get_guild_commands(self, guild_id: int) -> dict:
        self.ensure_guild_entry(guild_id)
        return self.data[str(guild_id)]["commands"]

    def save(self) -> None:
        save_data(self.data)

    def normalize_name(self, name: str) -> str:
        return name.strip().lower()

    def is_reserved_command(self, name: str) -> bool:
        name = self.normalize_name(name)

        # Prevent collisions with real bot commands
        if self.bot.get_command(name) is not None:
            return True

        # Prevent collisions with slash command names you already use
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
            "purge",
            "colors",
            "alert",
        }
        return name in reserved

    # ----------------------------
    # COMMAND GROUP
    # ----------------------------
    customcommand = app_commands.Group(
        name="customcommand",
        description="Create and manage simple custom commands for your server"
    )

    @customcommand.command(name="add", description="Add a custom command")
    @app_commands.describe(
        name="The command name users will type with !",
        response="What the bot should say"
    )
    async def add(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 32],
        response: app_commands.Range[str, 1, 500]
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
                "❌ You need **Manage Server** to use custom commands.",
                ephemeral=True
            )
            return

        name = self.normalize_name(name)
        guild_commands = self.get_guild_commands(interaction.guild.id)

        if not name.isalnum():
            await interaction.response.send_message(
                "❌ Command names must only use letters and numbers.",
                ephemeral=True
            )
            return

        if self.is_reserved_command(name):
            await interaction.response.send_message(
                "❌ That command name is reserved or already used by the bot.",
                ephemeral=True
            )
            return

        if name in guild_commands:
            await interaction.response.send_message(
                "❌ That custom command already exists.",
                ephemeral=True
            )
            return

        if len(guild_commands) >= MAX_CUSTOM_COMMANDS_PER_GUILD:
            await interaction.response.send_message(
                f"❌ Free tier allows up to **{MAX_CUSTOM_COMMANDS_PER_GUILD}** custom commands per server.",
                ephemeral=True
            )
            return

        guild_commands[name] = {
            "response": response.strip(),
            "created_by": interaction.user.id
        }
        self.save()

        embed = discord.Embed(
            title="✅ Custom Command Added",
            color=discord.Color.green()
        )
        embed.add_field(name="Command", value=f"`!{name}`", inline=False)
        embed.add_field(name="Response", value=response.strip(), inline=False)
        embed.set_footer(
            text=f"{len(guild_commands)}/{MAX_CUSTOM_COMMANDS_PER_GUILD} free-tier custom commands used"
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @customcommand.command(name="edit", description="Edit an existing custom command")
    @app_commands.describe(
        name="The existing custom command name",
        new_name="Optional new command name",
        new_response="Optional new response"
    )
    async def edit(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 32],
        new_name: str | None = None,
        new_response: str | None = None
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
                "❌ You need **Manage Server** to use custom commands.",
                ephemeral=True
            )
            return

        guild_commands = self.get_guild_commands(interaction.guild.id)
        name = self.normalize_name(name)

        if name not in guild_commands:
            await interaction.response.send_message(
                "❌ That custom command does not exist.",
                ephemeral=True
            )
            return

        if new_name is None and new_response is None:
            await interaction.response.send_message(
                "❌ You need to provide a new name or a new response.",
                ephemeral=True
            )
            return

        old_data = guild_commands[name]
        final_name = name

        if new_name is not None:
            new_name = self.normalize_name(new_name)

            if not new_name.isalnum():
                await interaction.response.send_message(
                    "❌ New command names must only use letters and numbers.",
                    ephemeral=True
                )
                return

            if self.is_reserved_command(new_name):
                await interaction.response.send_message(
                    "❌ That new command name is reserved or already used by the bot.",
                    ephemeral=True
                )
                return

            if new_name != name and new_name in guild_commands:
                await interaction.response.send_message(
                    "❌ Another custom command already uses that name.",
                    ephemeral=True
                )
                return

            final_name = new_name

        final_response = new_response.strip() if new_response is not None else old_data["response"]

        # Remove old entry if renaming
        if final_name != name:
            del guild_commands[name]

        guild_commands[final_name] = {
            "response": final_response,
            "created_by": old_data.get("created_by", interaction.user.id)
        }
        self.save()

        embed = discord.Embed(
            title="✏️ Custom Command Updated",
            color=discord.Color.blurple()
        )
        embed.add_field(name="Command", value=f"`!{final_name}`", inline=False)
        embed.add_field(name="Response", value=final_response, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @customcommand.command(name="remove", description="Remove a custom command")
    @app_commands.describe(name="The custom command name to remove")
    async def remove(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 32]
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
                "❌ You need **Manage Server** to use custom commands.",
                ephemeral=True
            )
            return

        guild_commands = self.get_guild_commands(interaction.guild.id)
        name = self.normalize_name(name)

        if name not in guild_commands:
            await interaction.response.send_message(
                "❌ That custom command does not exist.",
                ephemeral=True
            )
            return

        del guild_commands[name]
        self.save()

        await interaction.response.send_message(
            f"🗑️ Removed custom command `!{name}`.",
            ephemeral=True
        )

    @customcommand.command(name="list", description="List all custom commands in this server")
    async def list_commands(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.",
                ephemeral=True
            )
            return

        guild_commands = self.get_guild_commands(interaction.guild.id)

        if not guild_commands:
            await interaction.response.send_message(
                "This server has no custom commands yet.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🧩 Custom Commands",
            description="Here are the custom commands for this server.",
            color=discord.Color.blurple()
        )

        lines = []
        for name, data in list(guild_commands.items())[:15]:
            lines.append(
                f"**`!{name}`**\n{data['response']}"
            )

        embed.description = "\n\n".join(lines)

        if len(guild_commands) > 15:
            embed.set_footer(text=f"Showing 15 of {len(guild_commands)} custom commands")
        else:
            embed.set_footer(
                text=f"{len(guild_commands)}/{MAX_CUSTOM_COMMANDS_PER_GUILD} free-tier custom commands used"
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ----------------------------
    # MESSAGE LISTENER
    # ----------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return

        if message.author.bot:
            return

        content = message.content.strip()
        if not content.startswith("!"):
            return

        if len(content) <= 1:
            return

        parts = content[1:].split()
        if not parts:
            return

        command_name = self.normalize_name(parts[0])
        guild_commands = self.get_guild_commands(message.guild.id)

        if command_name not in guild_commands:
            return

        response = guild_commands[command_name]["response"]

        # Simple variable replacement
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