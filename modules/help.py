from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

import discord
from discord import app_commands
from discord.ext import commands

from core.command_catalog import build_command_catalog


def _walk_tree_commands(
    commands_list: Iterable[app_commands.Command | app_commands.Group],
) -> list[app_commands.Command]:
    collected: list[app_commands.Command] = []
    for command in commands_list:
        if isinstance(command, app_commands.Group):
            collected.extend(_walk_tree_commands(command.commands))
            continue
        collected.append(command)
    return collected


class HelpCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _catalog(self) -> list[dict]:
        return build_command_catalog(self.bot)

    def _find_command(self, command_name: str) -> app_commands.Command | None:
        normalized = str(command_name or "").strip().lower()
        if not normalized:
            return None
        for command in _walk_tree_commands(self.bot.tree.get_commands(guild=None)):
            if command.qualified_name.lower() == normalized:
                return command
        return None

    async def _command_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        query = current.lower().strip()
        matches: list[app_commands.Choice[str]] = []
        for item in self._catalog():
            if query and query not in item["name"].lower():
                continue
            matches.append(app_commands.Choice(name=f"/{item['name']}", value=item["name"]))
            if len(matches) >= 25:
                break
        return matches

    def _parameter_lines(self, command: app_commands.Command) -> list[str]:
        raw_parameters = getattr(command, "parameters", [])
        if isinstance(raw_parameters, dict):
            parameters = list(raw_parameters.values())
        else:
            parameters = list(raw_parameters)

        lines: list[str] = []
        for parameter in parameters:
            display_name = getattr(parameter, "display_name", None) or getattr(parameter, "name", "option")
            description = getattr(parameter, "description", None) or "No description yet."
            required = bool(getattr(parameter, "required", False))
            label = "Required" if required else "Optional"

            extras: list[str] = []
            choices = getattr(parameter, "choices", None) or []
            if choices:
                choice_names = ", ".join(str(getattr(choice, "name", choice)) for choice in list(choices)[:5])
                extras.append(f"choices: {choice_names}")

            min_value = getattr(parameter, "min_value", None)
            max_value = getattr(parameter, "max_value", None)
            if min_value is not None or max_value is not None:
                extras.append(f"range: {min_value if min_value is not None else '-'} to {max_value if max_value is not None else '-'}")

            min_length = getattr(parameter, "min_length", None)
            max_length = getattr(parameter, "max_length", None)
            if min_length is not None or max_length is not None:
                extras.append(f"length: {min_length if min_length is not None else '-'} to {max_length if max_length is not None else '-'}")

            extra_text = f" ({'; '.join(extras)})" if extras else ""
            lines.append(f"`{display_name}` [{label}] - {description}{extra_text}")
        return lines

    def _usage_text(self, command: app_commands.Command) -> str:
        raw_parameters = getattr(command, "parameters", [])
        if isinstance(raw_parameters, dict):
            parameters = list(raw_parameters.values())
        else:
            parameters = list(raw_parameters)

        parts = [f"/{command.qualified_name}"]
        for parameter in parameters:
            display_name = getattr(parameter, "display_name", None) or getattr(parameter, "name", "option")
            required = bool(getattr(parameter, "required", False))
            parts.append(f"<{display_name}>" if required else f"[{display_name}]")
        return " ".join(parts)

    @app_commands.command(name="help", description="Get help for a specific command")
    @app_commands.describe(command_name="Pick the command you want help with")
    @app_commands.autocomplete(command_name=_command_autocomplete)
    async def help_command(
        self,
        interaction: discord.Interaction,
        command_name: str | None = None,
    ) -> None:
        if not command_name:
            catalog = self._catalog()
            module_counts: dict[str, int] = defaultdict(int)
            for item in catalog:
                module_counts[item["module"]] += 1

            top_sections = sorted(module_counts.items(), key=lambda item: (-item[1], item[0]))[:6]
            embed = discord.Embed(
                title="ServerCore help",
                description="Pick a command in `/help` autocomplete to see what it does, what options it takes, and how to use it.",
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Quick start", value="Use `/help` and start typing the command name you want help with.", inline=False)
            embed.add_field(
                name="Browse everything",
                value="Use `/commandlist` to see the full command list grouped by section.",
                inline=False,
            )
            embed.add_field(
                name="Biggest sections",
                value="\n".join(f"`{name}` - {count} commands" for name, count in top_sections) or "No commands found.",
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        command = self._find_command(command_name)
        if command is None:
            await interaction.response.send_message("I couldn't find that command. Try picking it from `/help` autocomplete.", ephemeral=True)
            return

        catalog_entry = next((item for item in self._catalog() if item["name"].lower() == command.qualified_name.lower()), None)
        embed = discord.Embed(
            title=f"Help: /{command.qualified_name}",
            description=command.description or "No description yet.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Usage", value=f"`{self._usage_text(command)}`", inline=False)
        if catalog_entry is not None:
            embed.add_field(name="Section", value=catalog_entry["module"], inline=True)
            embed.add_field(name="Plan", value=catalog_entry["tier"], inline=True)

        parameter_lines = self._parameter_lines(command)
        embed.add_field(
            name="Options",
            value="\n".join(parameter_lines)[:1024] if parameter_lines else "This command has no options.",
            inline=False,
        )
        embed.set_footer(text="Tip: required options use < > and optional ones use [ ].")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="commandlist", description="List all ServerCore commands")
    async def commandlist(self, interaction: discord.Interaction) -> None:
        catalog = self._catalog()
        grouped: dict[str, list[dict]] = defaultdict(list)
        for item in catalog:
            grouped[item["module"]].append(item)

        embeds: list[discord.Embed] = []
        current_embed = discord.Embed(
            title="ServerCore command list",
            description="All slash commands grouped by section.",
            color=discord.Color.blurple(),
        )
        field_count = 0

        for module_name in sorted(grouped):
            lines = [f"`/{item['name']}` - {item['description']}" for item in grouped[module_name]]
            value = "\n".join(lines)
            if len(value) > 1024:
                trimmed_lines: list[str] = []
                total = 0
                for line in lines:
                    if total + len(line) + 1 > 980:
                        trimmed_lines.append("`...` - More commands are available in this section.")
                        break
                    trimmed_lines.append(line)
                    total += len(line) + 1
                value = "\n".join(trimmed_lines)

            if field_count >= 6:
                embeds.append(current_embed)
                current_embed = discord.Embed(color=discord.Color.blurple())
                field_count = 0

            current_embed.add_field(name=module_name, value=value or "No commands.", inline=False)
            field_count += 1

        if field_count or not embeds:
            embeds.append(current_embed)

        for index, embed in enumerate(embeds, start=1):
            embed.set_footer(text=f"Page {index} of {len(embeds)}")

        await interaction.response.send_message(embeds=embeds[:10], ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCommands(bot))
