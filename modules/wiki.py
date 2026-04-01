import discord
import httpx
from discord import app_commands
from discord.ext import commands

WIKIPEDIA_RANDOM_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/random/summary"


def _trim_summary(text: str, limit: int = 900) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


class Wiki(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="wiki",
        description="Get a random Wikipedia article with a quick summary",
    )
    async def wiki(self, interaction: discord.Interaction):
        await interaction.response.defer()

        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(
                    WIKIPEDIA_RANDOM_SUMMARY_URL,
                    headers={"User-Agent": "ServerCore Discord Bot"},
                )
                response.raise_for_status()
                payload = response.json()
        except Exception:
            await interaction.followup.send(
                "I couldn't reach Wikipedia right now. Please try again in a moment.",
                ephemeral=True,
            )
            return

        title = payload.get("title") or "Random Wikipedia Article"
        summary = _trim_summary(
            payload.get("extract")
            or "Wikipedia did not return a summary for this article."
        )

        article_url = (
            payload.get("content_urls", {})
            .get("desktop", {})
            .get("page")
        ) or "https://en.wikipedia.org/wiki/Special:Random"

        embed = discord.Embed(
            title=title,
            description=summary,
            color=discord.Color.blurple(),
            url=article_url,
        )
        embed.add_field(
            name="Open article",
            value=f"[Read the full Wikipedia page]({article_url})",
            inline=False,
        )

        thumbnail = payload.get("thumbnail", {}).get("source")
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)

        page_type = payload.get("type")
        if page_type and page_type != "standard":
            embed.set_footer(text=f"Wikipedia page type: {page_type}")
        else:
            embed.set_footer(text="Random article from Wikipedia")

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Wiki(bot))
