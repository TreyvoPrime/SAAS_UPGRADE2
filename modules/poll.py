import json
import time
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

DATA_FILE = Path("polls.json")


def load_data() -> dict:
    if not DATA_FILE.exists():
        return {}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_data(data: dict) -> None:
    with DATA_FILE.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=4)


def parse_duration(time_value: int, unit: str) -> int:
    unit = unit.lower()
    if unit in {"minutes", "minute"}:
        return time_value * 60
    if unit in {"hours", "hour"}:
        return time_value * 3600
    if unit in {"days", "day"}:
        return time_value * 86400
    return 0


def progress_bar(percent: float, length: int = 10) -> str:
    if percent <= 0:
        return "-" * length
    filled = round((percent / 100) * length)
    filled = max(0, min(length, filled))
    return "#" * filled + "." * (length - filled)


class PollButton(discord.ui.Button):
    def __init__(self, cog: "Poll", poll_id: str, option_index: int, label: str, style: discord.ButtonStyle):
        super().__init__(label=label[:80], style=style, custom_id=f"poll:{poll_id}:{option_index}")
        self.cog = cog
        self.poll_id = poll_id
        self.option_index = option_index

    async def callback(self, interaction: discord.Interaction):
        poll = self.cog.get_poll(self.poll_id)
        if poll is None:
            await interaction.response.send_message("This poll no longer exists.", ephemeral=True)
            return
        if poll.get("closed", False):
            await interaction.response.send_message("This poll has already ended.", ephemeral=True)
            return
        if time.time() >= poll["end_time"]:
            await self.cog.close_poll(self.poll_id)
            await interaction.response.send_message("This poll has already ended.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        votes = poll.setdefault("votes", {})
        previous_vote = votes.get(user_id)
        if previous_vote is not None and previous_vote == self.option_index:
            await interaction.response.send_message("You already voted for that choice.", ephemeral=True)
            return

        votes[user_id] = self.option_index
        self.cog.save()
        await interaction.response.edit_message(embed=self.cog.build_poll_embed(poll), view=self.cog.build_poll_view(poll))


class PollView(discord.ui.View):
    def __init__(self, cog: "Poll", poll: dict):
        super().__init__(timeout=None)
        styles = [
            discord.ButtonStyle.primary,
            discord.ButtonStyle.secondary,
            discord.ButtonStyle.success,
            discord.ButtonStyle.danger,
            discord.ButtonStyle.primary,
        ]
        for index, option in enumerate(poll["options"]):
            self.add_item(PollButton(cog=cog, poll_id=poll["id"], option_index=index, label=option, style=styles[index % len(styles)]))


class Poll(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = load_data()
        self.poll_watcher.start()

    async def cog_load(self):
        self.register_persistent_views()

    def cog_unload(self):
        self.poll_watcher.cancel()

    def save(self):
        save_data(self.data)

    def get_polls(self) -> dict:
        return self.data.setdefault("polls", {})

    def get_poll(self, poll_id: str) -> Optional[dict]:
        return self.get_polls().get(str(poll_id))

    def next_poll_id(self) -> str:
        polls = self.get_polls()
        ids = [int(key) for key in polls if str(key).isdigit()]
        return str(max(ids, default=0) + 1)

    def register_persistent_views(self):
        for poll in self.get_polls().values():
            if not poll.get("closed", False):
                try:
                    self.bot.add_view(self.build_poll_view(poll))
                except Exception:
                    pass

    def build_poll_view(self, poll: dict) -> PollView:
        return PollView(self, poll)

    def get_vote_counts(self, poll: dict) -> list[int]:
        counts = [0 for _ in poll["options"]]
        for option_index in poll.get("votes", {}).values():
            if 0 <= option_index < len(counts):
                counts[option_index] += 1
        return counts

    def get_winners(self, poll: dict) -> tuple[list[int], int]:
        counts = self.get_vote_counts(poll)
        if not counts:
            return [], 0
        max_votes = max(counts, default=0)
        if max_votes == 0:
            return [], 0
        winners = [index for index, count in enumerate(counts) if count == max_votes]
        return winners, max_votes

    def build_poll_embed(self, poll: dict) -> discord.Embed:
        counts = self.get_vote_counts(poll)
        total_votes = sum(counts)
        ended = poll.get("closed", False) or time.time() >= poll["end_time"]
        anonymous = bool(poll.get("anonymous", False))

        embed = discord.Embed(
            title=f"Poll: {poll['question']}",
            description=(
                "Choose one of the buttons below to vote. You can change your vote until the poll ends."
                if not ended
                else "This poll has ended. Final results are shown below."
            ),
            color=discord.Color.blurple() if not ended else discord.Color.dark_grey(),
        )

        option_lines: list[str] = []
        for index, option in enumerate(poll["options"]):
            votes = counts[index]
            percent = (votes / total_votes * 100) if total_votes > 0 else 0
            if anonymous and not ended:
                stats_text = "`Results stay hidden until the poll ends`"
            elif total_votes == 0:
                stats_text = "`No votes yet`"
            else:
                bar = progress_bar(percent)
                stats_text = f"`{bar}` {votes} vote(s) | {percent:.0f}%"
            option_lines.append(f"**Choice {index + 1}: {option}**\n{stats_text}")

        embed.add_field(name="Choices", value="\n\n".join(option_lines), inline=False)

        creator_mention = f"<@{poll['author_id']}>"
        end_timestamp = int(poll["end_time"])
        mode_label = "Anonymous" if anonymous else "Live results"

        if ended:
            winners, max_votes = self.get_winners(poll)
            if not winners:
                winner_text = "No one voted in this poll."
            elif len(winners) == 1:
                winner_text = f"Winner: Choice {winners[0] + 1} - **{poll['options'][winners[0]]}** with `{max_votes}` vote(s)"
            else:
                tied_choices = ", ".join(f"Choice {idx + 1} - **{poll['options'][idx]}**" for idx in winners)
                winner_text = f"Tie: {tied_choices} with `{max_votes}` vote(s) each"
            embed.add_field(name="Final Result", value=winner_text, inline=False)
            embed.add_field(
                name="Poll Details",
                value=(
                    f"**Poll ID:** `{poll['id']}`\n"
                    f"**Created by:** {creator_mention}\n"
                    f"**Ended:** <t:{end_timestamp}:F>\n"
                    f"**Total Votes:** `{total_votes}`\n"
                    f"**Mode:** `{mode_label}`"
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="Poll Details",
                value=(
                    f"**Poll ID:** `{poll['id']}`\n"
                    f"**Created by:** {creator_mention}\n"
                    f"**Ends:** <t:{end_timestamp}:F>\n"
                    f"**Time Left:** <t:{end_timestamp}:R>\n"
                    f"**Total Votes:** `{total_votes}`\n"
                    f"**Mode:** `{mode_label}`"
                ),
                inline=False,
            )
        return embed

    async def fetch_poll_message(self, poll: dict) -> Optional[discord.Message]:
        channel = self.bot.get_channel(poll["channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(poll["channel_id"])
            except Exception:
                return None
        try:
            return await channel.fetch_message(poll["message_id"])
        except Exception:
            return None

    async def close_poll(self, poll_id: str):
        poll = self.get_poll(poll_id)
        if poll is None or poll.get("closed", False):
            return
        poll["closed"] = True
        self.save()
        message = await self.fetch_poll_message(poll)
        if message is not None:
            ended_view = self.build_poll_view(poll)
            for child in ended_view.children:
                child.disabled = True
            try:
                await message.edit(embed=self.build_poll_embed(poll), view=ended_view)
            except discord.HTTPException:
                pass

    @tasks.loop(seconds=15)
    async def poll_watcher(self):
        now = time.time()
        for poll in list(self.get_polls().values()):
            if not poll.get("closed", False) and now >= poll["end_time"]:
                await self.close_poll(poll["id"])

    @poll_watcher.before_loop
    async def before_poll_watcher(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="poll", description="Create a poll with up to 5 choices and a clear time setting")
    @app_commands.describe(
        question="What is the poll asking?",
        time="How long the poll should run",
        unit="Choose minutes, hours, or days",
        option1="First choice",
        option2="Second choice",
        option3="Third choice (optional)",
        option4="Fourth choice (optional)",
        option5="Fifth choice (optional)",
        anonymous="Hide live results until the poll ends",
    )
    @app_commands.choices(
        unit=[
            app_commands.Choice(name="minutes", value="minutes"),
            app_commands.Choice(name="hours", value="hours"),
            app_commands.Choice(name="days", value="days"),
        ]
    )
    async def poll(
        self,
        interaction: discord.Interaction,
        question: app_commands.Range[str, 1, 200],
        time: app_commands.Range[int, 1, 1000],
        unit: app_commands.Choice[str],
        option1: app_commands.Range[str, 1, 80],
        option2: app_commands.Range[str, 1, 80],
        option3: str | None = None,
        option4: str | None = None,
        option5: str | None = None,
        anonymous: bool = False,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        duration = parse_duration(time, unit.value)
        if duration <= 0:
            await interaction.response.send_message("That time length is not valid.", ephemeral=True)
            return

        options = [option1.strip(), option2.strip()]
        for choice in [option3, option4, option5]:
            if choice is not None and choice.strip():
                options.append(choice.strip())
        if len(options) < 2:
            await interaction.response.send_message("A poll needs at least 2 choices.", ephemeral=True)
            return

        poll_id = self.next_poll_id()
        end_time = int(time_module() + duration)
        poll = {
            "id": poll_id,
            "guild_id": interaction.guild.id,
            "channel_id": interaction.channel.id,
            "message_id": None,
            "author_id": interaction.user.id,
            "question": question,
            "options": options,
            "votes": {},
            "anonymous": bool(anonymous),
            "created_at": int(time_module()),
            "end_time": end_time,
            "closed": False,
        }
        self.get_polls()[poll_id] = poll
        self.save()

        embed = self.build_poll_embed(poll)
        embed.set_author(name=f"Poll by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        view = self.build_poll_view(poll)
        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()
        poll["message_id"] = message.id
        self.save()
        self.bot.add_view(self.build_poll_view(poll))

    @app_commands.command(name="pollresults", description="See the current or final results of a poll")
    @app_commands.describe(poll_id="The poll ID you want to check")
    async def pollresults(self, interaction: discord.Interaction, poll_id: str):
        poll = self.get_poll(poll_id)
        if poll is None:
            await interaction.response.send_message("Poll not found.", ephemeral=True)
            return
        await interaction.response.send_message(embed=self.build_poll_embed(poll), ephemeral=True)

    @app_commands.command(name="endpoll", description="End a poll before its timer runs out")
    @app_commands.describe(poll_id="The poll ID to end early")
    async def endpoll(self, interaction: discord.Interaction, poll_id: str):
        poll = self.get_poll(poll_id)
        if poll is None:
            await interaction.response.send_message("Poll not found.", ephemeral=True)
            return
        is_creator = interaction.user.id == poll["author_id"]
        is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild
        if not is_creator and not is_admin:
            await interaction.response.send_message("Only the poll creator or a server admin can end this poll.", ephemeral=True)
            return
        if poll.get("closed", False):
            await interaction.response.send_message("This poll is already closed.", ephemeral=True)
            return
        poll["end_time"] = int(time_module())
        await self.close_poll(poll_id)
        await interaction.response.send_message(f"Poll `{poll_id}` has been ended.", ephemeral=True)


def time_module():
    return time.time()


async def setup(bot: commands.Bot):
    await bot.add_cog(Poll(bot))
