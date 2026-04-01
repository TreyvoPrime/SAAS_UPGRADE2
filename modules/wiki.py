from __future__ import annotations

from urllib.parse import quote

import discord
import httpx
from discord import app_commands
from discord.ext import commands

WIKIPEDIA_RANDOM_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/random/summary"
WIKIPEDIA_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKIPEDIA_SEARCH_URL = "https://en.wikipedia.org/w/rest.php/v1/search/title"
WIKIPEDIA_FALLBACK_URL = "https://en.wikipedia.org/wiki/Special:Random"
WIKIPEDIA_USER_AGENT = "ServerCore Discord Bot"


def _trim_summary(text: str, limit: int = 900) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


class Wiki(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _fetch_random_article(self) -> dict | None:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(
                WIKIPEDIA_RANDOM_SUMMARY_URL,
                headers={"User-Agent": WIKIPEDIA_USER_AGENT},
            )
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else None

    async def _search_title(self, query: str) -> str | None:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(
                WIKIPEDIA_SEARCH_URL,
                params={"q": query, "limit": 1},
                headers={"User-Agent": WIKIPEDIA_USER_AGENT},
            )
            response.raise_for_status()
            payload = response.json()
        pages = payload.get("pages", []) if isinstance(payload, dict) else []
        if not pages:
            return None
        return str(pages[0].get("title") or "").strip() or None

    async def _fetch_summary(self, title: str) -> dict | None:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(
                WIKIPEDIA_SUMMARY_URL.format(title=quote(title, safe="")),
                headers={"User-Agent": WIKIPEDIA_USER_AGENT},
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else None

    def _build_embed(self, payload: dict, *, searched_query: str | None = None) -> discord.Embed:
        title = payload.get("title") or "Wikipedia article"
        summary = _trim_summary(payload.get("extract") or "Wikipedia did not return a summary for this article.")
        article_url = (
            payload.get("content_urls", {}).get("desktop", {}).get("page")
        ) or WIKIPEDIA_FALLBACK_URL

        embed = discord.Embed(
            title=title,
            description=summary,
            color=discord.Color.blurple(),
            url=article_url,
        )
        embed.add_field(name="Open article", value=f"[Read the full Wikipedia page]({article_url})", inline=False)

        thumbnail = payload.get("thumbnail", {}).get("source")
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)

        page_type = payload.get("type")
        if searched_query:
            embed.set_footer(text=f"Best Wikipedia match for: {searched_query}")
        elif page_type and page_type != "standard":
            embed.set_footer(text=f"Wikipedia page type: {page_type}")
        else:
            embed.set_footer(text="Random article from Wikipedia")
        return embed

    @app_commands.command(name="wiki", description="Get a random Wikipedia article or search for something specific")
    @app_commands.describe(query="Optional: search Wikipedia instead of pulling a random article")
    async def wiki(
        self,
        interaction: discord.Interaction,
        query: app_commands.Range[str, 1, 120] | None = None,
    ) -> None:
        await interaction.response.defer()

        try:
            if query:
                title = await self._search_title(query)
                if not title:
                    await interaction.followup.send("I couldn't find a Wikipedia article that matched that search.", ephemeral=True)
                    return
                payload = await self._fetch_summary(title)
            else:
                payload = await self._fetch_random_article()
        except Exception:
            await interaction.followup.send("I couldn't reach Wikipedia right now. Please try again in a moment.", ephemeral=True)
            return

        if not payload:
            await interaction.followup.send("Wikipedia did not return a usable result for that request.", ephemeral=True)
            return

        embed = self._build_embed(payload, searched_query=query)
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Wiki(bot))
