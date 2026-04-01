from __future__ import annotations

from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands


def _display_name(user: discord.abc.User) -> str:
    return getattr(user, "display_name", None) or user.name


def _format_timestamp(dt: datetime | None) -> str:
    if dt is None:
        return "Unknown"
    unix = int(dt.timestamp())
    return f"<t:{unix}:F>\n<t:{unix}:R>"


class Profile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _avatar_options(self, target: discord.Member | discord.User) -> list[tuple[str, str]]:
        options: list[tuple[str, str]] = [("Display avatar", target.display_avatar.url)]
        if target.avatar:
            options.append(("Global avatar", target.avatar.url))
        if isinstance(target, discord.Member) and target.guild_avatar:
            options.append(("Server avatar", target.guild_avatar.url))
        return options

    async def _fetch_profile_user(self, user_id: int) -> discord.User | None:
        try:
            return await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            return None

    @app_commands.command(name="avatar", description="View a member's avatar with more control")
    @app_commands.describe(
        user="The user whose avatar you want to view",
        scope="Choose which avatar you want to open",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="Display avatar", value="display"),
            app_commands.Choice(name="Global avatar", value="global"),
            app_commands.Choice(name="Server avatar", value="server"),
            app_commands.Choice(name="All available links", value="all"),
        ]
    )
    async def avatar(
        self,
        interaction: discord.Interaction,
        user: discord.Member | discord.User | None = None,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        target = user or interaction.user
        selected_scope = scope.value if scope else "display"
        avatar_options = self._avatar_options(target)
        option_map = {label.lower().replace(" ", "_"): (label, url) for label, url in avatar_options}

        if selected_scope == "server" and "server_avatar" not in option_map:
            await interaction.response.send_message("That user does not have a separate server avatar here.", ephemeral=True)
            return
        if selected_scope == "global" and "global_avatar" not in option_map:
            await interaction.response.send_message("That user does not have a separate global avatar set.", ephemeral=True)
            return

        if selected_scope == "all":
            primary_label, primary_url = avatar_options[0]
            embed = discord.Embed(
                title=f"Avatar links for {_display_name(target)}",
                description="Open the image version you want to use.",
                color=discord.Color.blurple(),
            )
            embed.set_image(url=primary_url)
            embed.add_field(
                name="Available images",
                value="\n".join(f"[{label}]({url})" for label, url in avatar_options),
                inline=False,
            )
        else:
            if selected_scope == "global":
                label, url = option_map["global_avatar"]
            elif selected_scope == "server":
                label, url = option_map["server_avatar"]
            else:
                label, url = option_map["display_avatar"]
            embed = discord.Embed(
                title=f"{label} for {_display_name(target)}",
                color=discord.Color.blurple(),
                url=url,
            )
            embed.set_image(url=url)
            embed.add_field(name="Open image", value=f"[Open this avatar]({url})", inline=False)

        embed.set_footer(text=f"Requested by {_display_name(interaction.user)}", icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="banner", description="View a user's banner or accent color")
    @app_commands.describe(user="The user whose banner you want to view")
    async def banner(
        self,
        interaction: discord.Interaction,
        user: discord.Member | discord.User | None = None,
    ) -> None:
        target = user or interaction.user
        fetched_user = await self._fetch_profile_user(target.id)
        if fetched_user is None:
            await interaction.response.send_message("I couldn't fetch that user's profile data right now.", ephemeral=True)
            return

        accent = getattr(fetched_user, "accent_color", None) or getattr(fetched_user, "accent_colour", None)
        if fetched_user.banner is None:
            accent_text = str(accent) if accent else "No accent color set"
            await interaction.response.send_message(
                f"{_display_name(fetched_user)} does not have a banner. Accent color: `{accent_text}`.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"Banner for {fetched_user.name}",
            description="Open the banner in full size if you want the clean image file.",
            color=accent or discord.Color.blurple(),
            url=fetched_user.banner.url,
        )
        embed.set_image(url=fetched_user.banner.url)
        embed.add_field(name="Banner link", value=f"[Open banner]({fetched_user.banner.url})", inline=False)
        if accent:
            embed.add_field(name="Accent color", value=f"`{accent}`", inline=True)
        embed.set_footer(text=f"Requested by {_display_name(interaction.user)}", icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="profilecard", description="See a clean profile summary with avatar, banner, and account details")
    @app_commands.describe(user="The user you want to look up")
    async def profilecard(
        self,
        interaction: discord.Interaction,
        user: discord.Member | discord.User | None = None,
    ) -> None:
        target = user or interaction.user
        fetched_user = await self._fetch_profile_user(target.id)
        embed_color = discord.Color.blurple()
        accent = None
        if fetched_user is not None:
            accent = getattr(fetched_user, "accent_color", None) or getattr(fetched_user, "accent_colour", None)
            if accent:
                embed_color = accent
        if isinstance(target, discord.Member) and target.color != discord.Color.default():
            embed_color = target.color

        embed = discord.Embed(
            title=f"Profile card for {_display_name(target)}",
            description="A quick visual summary you can use without digging through user settings.",
            color=embed_color,
        )
        embed.set_thumbnail(url=target.display_avatar.url)

        embed.add_field(name="Username", value=str(target), inline=True)
        embed.add_field(name="Display name", value=_display_name(target), inline=True)
        embed.add_field(name="User ID", value=f"`{target.id}`", inline=True)
        embed.add_field(name="Account created", value=_format_timestamp(target.created_at), inline=False)

        avatar_links = [f"[{label}]({url})" for label, url in self._avatar_options(target)]
        embed.add_field(name="Avatar links", value="\n".join(avatar_links), inline=False)

        if isinstance(target, discord.Member):
            embed.add_field(name="Top role", value=target.top_role.mention, inline=True)
            embed.add_field(name="Joined server", value=_format_timestamp(target.joined_at), inline=True)
            embed.add_field(name="Server avatar", value="Yes" if target.guild_avatar else "No", inline=True)

        if fetched_user and fetched_user.banner:
            embed.set_image(url=fetched_user.banner.url)
            embed.add_field(name="Banner", value=f"[Open banner]({fetched_user.banner.url})", inline=False)
        elif accent:
            embed.add_field(name="Accent color", value=f"`{accent}`", inline=False)

        embed.set_footer(text=f"Requested by {_display_name(interaction.user)}", icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Profile(bot))
