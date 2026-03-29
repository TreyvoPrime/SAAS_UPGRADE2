import discord
from discord.ext import commands
from discord import app_commands

# Role names + actual Discord colors
COLOR_MAP = {
    "Red": discord.Color.red(),
    "Orange": discord.Color.orange(),
    "Yellow": discord.Color.yellow(),
    "Green": discord.Color.green(),
    "Blue": discord.Color.blue(),
    "Purple": discord.Color.purple(),
    "Pink": discord.Color.from_rgb(255, 105, 180),
}

COLOR_ROLE_NAMES = list(COLOR_MAP.keys())


async def ensure_color_roles(guild: discord.Guild) -> list[discord.Role]:
    """
    Create missing color roles in the server.
    Returns the list of color roles that exist after creation.
    """
    existing_roles = {role.name: role for role in guild.roles}
    created_or_found = []

    for role_name, role_color in COLOR_MAP.items():
        role = existing_roles.get(role_name)

        if role is None:
            role = await guild.create_role(
                name=role_name,
                colour=role_color,
                reason="Auto-creating color roles for color picker"
            )

        created_or_found.append(role)

    return created_or_found


class ColorSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Red", emoji="🔴", description="Set your name color to red"),
            discord.SelectOption(label="Orange", emoji="🟠", description="Set your name color to orange"),
            discord.SelectOption(label="Yellow", emoji="🟡", description="Set your name color to yellow"),
            discord.SelectOption(label="Green", emoji="🟢", description="Set your name color to green"),
            discord.SelectOption(label="Blue", emoji="🔵", description="Set your name color to blue"),
            discord.SelectOption(label="Purple", emoji="🟣", description="Set your name color to purple"),
            discord.SelectOption(label="Pink", emoji="🌸", description="Set your name color to pink"),
            discord.SelectOption(label="Remove Color", emoji="⚪", description="Remove your current color role"),
        ]

        super().__init__(
            placeholder="Choose your name color...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This can only be used in a server.",
                ephemeral=True
            )
            return

        guild = interaction.guild
        member = interaction.user

        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Could not load your server member data.",
                ephemeral=True
            )
            return

        bot_member = guild.me
        if bot_member is None:
            await interaction.response.send_message(
                "I couldn't verify my bot permissions in this server.",
                ephemeral=True
            )
            return

        # Bot needs Manage Roles to create/edit roles
        if not guild.me.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "I need the **Manage Roles** permission to create and assign color roles.",
                ephemeral=True
            )
            return

        chosen = self.values[0]

        try:
            # Make sure roles exist before doing anything else
            await ensure_color_roles(guild)

            # Refresh role lookup after creation
            guild_roles = {role.name: role for role in guild.roles}

            user_color_roles = [
                role for role in member.roles
                if role.name in COLOR_ROLE_NAMES
            ]

            if chosen == "Remove Color":
                if user_color_roles:
                    await member.remove_roles(*user_color_roles, reason="User removed color role")
                    await interaction.response.send_message(
                        "Your color role was removed.",
                        ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        "You don't have a color role right now.",
                        ephemeral=True
                    )
                return

            selected_role = guild_roles.get(chosen)
            if selected_role is None:
                await interaction.response.send_message(
                    "That color role could not be found or created.",
                    ephemeral=True
                )
                return

            # Role hierarchy check
            if selected_role >= bot_member.top_role:
                await interaction.response.send_message(
                    f"I can't assign **{selected_role.name}** because it is above my highest role.\n"
                    f"Move my bot role above the color roles in Server Settings > Roles.",
                    ephemeral=True
                )
                return

            # Remove old color roles first
            removable_roles = [
                role for role in user_color_roles
                if role != selected_role and role < bot_member.top_role
            ]

            if removable_roles:
                await member.remove_roles(*removable_roles, reason="Changing color role")

            # Add new role if they don't already have it
            if selected_role not in member.roles:
                await member.add_roles(selected_role, reason="Selected color role")

            await interaction.response.send_message(
                f"Your name color is now **{selected_role.name}**.",
                ephemeral=True
            )

        except discord.Forbidden:
            await interaction.response.send_message(
                "I was blocked by Discord permissions. Make sure I have **Manage Roles** and that my bot role is above the color roles.",
                ephemeral=True
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"Discord returned an error while updating your color: `{e}`",
                ephemeral=True
            )


class ColorView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(ColorSelect())


class Colors(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="color", description="Pick a color for your name")
    async def color(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command only works in servers.",
                ephemeral=True
            )
            return

        # Try creating roles before showing the menu
        try:
            if not interaction.guild.me.guild_permissions.manage_roles:
                await interaction.response.send_message(
                    "I need **Manage Roles** to auto-create color roles.",
                    ephemeral=True
                )
                return

            await ensure_color_roles(interaction.guild)

            await interaction.response.send_message(
                "Pick your name color from the dropdown:",
                view=ColorView(),
                ephemeral=True
            )

        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to create roles. Give me **Manage Roles**.",
                ephemeral=True
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"I couldn't create the roles: `{e}`",
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Colors(bot))