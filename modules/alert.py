import discord
from discord.ext import commands
from discord import app_commands
import asyncio
from datetime import datetime, timedelta


class Alert(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cooldowns = {}

    def is_admin(self, member: discord.Member):
        return (
            member.guild_permissions.administrator
            or member.guild_permissions.manage_guild
        )

    @app_commands.command(
        name="dmrole",
        description="DM all non-VC users in a role"
    )
    @app_commands.describe(
        role="Role to DM",
        message="Message to send",
        vc_link="Optional VC invite link"
    )
    async def dmrole(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        message: str,
        vc_link: str = None
    ):
        # ----------------------------
        # PERMISSION CHECK
        # ----------------------------
        if not self.is_admin(interaction.user):
            return await interaction.response.send_message(
                "❌ You don't have permission.",
                ephemeral=True
            )

        # ----------------------------
        # COOLDOWN SYSTEM
        # ----------------------------
        now = datetime.utcnow()

        if interaction.guild.id in self.cooldowns:
            if now < self.cooldowns[interaction.guild.id]:
                remaining = (self.cooldowns[interaction.guild.id] - now).seconds
                return await interaction.response.send_message(
                    f"⏳ Cooldown: {remaining}s",
                    ephemeral=True
                )

        self.cooldowns[interaction.guild.id] = now + timedelta(seconds=120)

        # ----------------------------
        # FILTER MEMBERS
        # ----------------------------
        members = [
            m for m in role.members
            if not m.bot and m.voice is None
        ]

        skipped_vc = len([m for m in role.members if m.voice is not None])

        if not members:
            return await interaction.response.send_message(
                "⚠️ No eligible members (all in VC or bots).",
                ephemeral=True
            )

        await interaction.response.send_message(
            f"📨 Sending to **{len(members)} users**...\n"
            f"🔇 Skipped (VC): {skipped_vc}",
            ephemeral=True
        )

        # ----------------------------
        # DM LOOP
        # ----------------------------
        sent = 0
        failed = 0

        for member in members:
            try:
                content = f"📢 **Message from {interaction.guild.name}**\n\n{message}"

                if vc_link:
                    content += f"\n\n🔊 Join VC: {vc_link}"

                await member.send(content)
                sent += 1

                await asyncio.sleep(0.3)  # anti-rate-limit

            except discord.Forbidden:
                failed += 1
            except discord.HTTPException:
                failed += 1

        # ----------------------------
        # RESULT
        # ----------------------------
        await interaction.followup.send(
            f"✅ Done!\nSent: {sent}\nFailed: {failed}\nSkipped (VC): {skipped_vc}",
            ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Alert(bot))