import json
import re
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks


DATA_FILE = Path("reminders.json")
TIME_TOKEN_PATTERN = re.compile(r"(\d+)\s*([smhd])", re.IGNORECASE)


def load_reminders() -> list:
    if not DATA_FILE.exists():
        return []

    try:
        with DATA_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_reminders(reminders: list) -> None:
    with DATA_FILE.open("w", encoding="utf-8") as handle:
        json.dump(reminders, handle, indent=4)


def parse_duration_input(value: str) -> int:
    cleaned = (value or "").strip().lower()
    if not cleaned:
        return 0

    total_seconds = 0
    consumed = ""

    for match in TIME_TOKEN_PATTERN.finditer(cleaned):
        amount = int(match.group(1))
        unit = match.group(2).lower()
        consumed += match.group(0)

        if unit == "s":
            total_seconds += amount
        elif unit == "m":
            total_seconds += amount * 60
        elif unit == "h":
            total_seconds += amount * 3600
        elif unit == "d":
            total_seconds += amount * 86400

    normalized_input = re.sub(r"\s+", "", cleaned)
    normalized_consumed = re.sub(r"\s+", "", consumed)
    if total_seconds <= 0 or normalized_input != normalized_consumed:
        return 0

    return total_seconds


class ReminderCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.reminders = load_reminders()
        self.reminder_task.start()

    def cog_unload(self):
        self.reminder_task.cancel()

    def get_next_id(self) -> int:
        if not self.reminders:
            return 1
        return max(reminder["id"] for reminder in self.reminders) + 1

    @tasks.loop(seconds=15)
    async def reminder_task(self):
        now = int(time.time())
        changed = False
        to_remove = []

        for reminder in self.reminders:
            if reminder["due_at"] <= now:
                user = self.bot.get_user(reminder["user_id"])

                if user is None:
                    try:
                        user = await self.bot.fetch_user(reminder["user_id"])
                    except Exception:
                        user = None

                if user is not None:
                    embed = discord.Embed(
                        title="Reminder",
                        description=reminder["message"],
                        color=discord.Color.blurple(),
                    )

                    if reminder.get("channel_id"):
                        embed.add_field(
                            name="Created In",
                            value=f"<#{reminder['channel_id']}>",
                            inline=False,
                        )

                    try:
                        await user.send(embed=embed)
                    except discord.HTTPException:
                        pass

                to_remove.append(reminder)
                changed = True

        if to_remove:
            for reminder in to_remove:
                if reminder in self.reminders:
                    self.reminders.remove(reminder)

        if changed:
            save_reminders(self.reminders)

    @reminder_task.before_loop
    async def before_reminder_task(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="remind", description="Set a reminder")
    @app_commands.describe(
        time_input="When the reminder should happen, like 10m, 2h, or 1d 4h",
        message="What you want to be reminded about",
    )
    async def remind(
        self,
        interaction: discord.Interaction,
        time_input: app_commands.Range[str, 1, 40],
        message: app_commands.Range[str, 1, 500],
    ):
        seconds = parse_duration_input(time_input)

        if seconds <= 0:
            await interaction.response.send_message(
                "Use a time like `10m`, `2h`, or `1d 4h 30m`.",
                ephemeral=True,
            )
            return

        due_at = int(time.time()) + seconds
        reminder_id = self.get_next_id()

        reminder = {
            "id": reminder_id,
            "user_id": interaction.user.id,
            "guild_id": interaction.guild.id if interaction.guild else None,
            "channel_id": interaction.channel.id if interaction.channel else None,
            "message": message,
            "created_at": int(time.time()),
            "due_at": due_at,
        }

        self.reminders.append(reminder)
        save_reminders(self.reminders)

        embed = discord.Embed(
            title="Reminder Set",
            color=discord.Color.green(),
        )
        embed.add_field(name="Reminder ID", value=str(reminder_id), inline=False)
        embed.add_field(name="Message", value=message, inline=False)
        embed.add_field(name="Due In", value=time_input, inline=False)
        embed.add_field(name="Due At", value=f"<t:{due_at}:F>\n<t:{due_at}:R>", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="myreminders", description="View your active reminders")
    async def myreminders(self, interaction: discord.Interaction):
        user_reminders = [
            reminder for reminder in self.reminders
            if reminder["user_id"] == interaction.user.id
        ]

        if not user_reminders:
            await interaction.response.send_message(
                "You have no active reminders.",
                ephemeral=True,
            )
            return

        user_reminders = sorted(user_reminders, key=lambda item: item["due_at"])

        embed = discord.Embed(
            title="Your Reminders",
            color=discord.Color.blurple(),
        )

        lines = []
        for reminder in user_reminders[:10]:
            lines.append(
                f"**ID {reminder['id']}** - {reminder['message']}\n"
                f"Due: <t:{reminder['due_at']}:F> (<t:{reminder['due_at']}:R>)"
            )

        embed.description = "\n\n".join(lines)

        if len(user_reminders) > 10:
            embed.set_footer(text=f"Showing 10 of {len(user_reminders)} reminders")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="removereminder", description="Remove one of your reminders")
    @app_commands.describe(reminder_id="The ID of the reminder you want to remove")
    async def removereminder(self, interaction: discord.Interaction, reminder_id: int):
        reminder_to_remove = None

        for reminder in self.reminders:
            if reminder["id"] == reminder_id and reminder["user_id"] == interaction.user.id:
                reminder_to_remove = reminder
                break

        if reminder_to_remove is None:
            await interaction.response.send_message(
                "Reminder not found, or it does not belong to you.",
                ephemeral=True,
            )
            return

        self.reminders.remove(reminder_to_remove)
        save_reminders(self.reminders)

        await interaction.response.send_message(
            f"Removed reminder `{reminder_id}`.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ReminderCog(bot))
