import os
import sys
from contextlib import suppress

import discord
from discord.ext import commands

from api.buttons import confirm_action
from api.commands_export import export_commands
from core.amenity import Amenity
from core.cache import cache


class Owner(commands.Cog):
    display_name = "Owner"
    group_name = "Owner"
    hidden = True
    owner_only = True

    def __init__(self, bot: Amenity) -> None:
        self.bot = bot

    def _format_bytes(self, value: int | float | None) -> str:
        if value is None:
            return "Unknown"
        size = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024:
                return f"{size:,.2f} {unit}"
            size /= 1024
        return f"{size:,.2f} PB"

    def _read_meminfo(self) -> tuple[int | None, int | None]:
        try:
            with open("/proc/meminfo", encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            return None, None

        values: dict[str, int] = {}
        for line in lines:
            parts = line.split(":", 1)
            if len(parts) != 2:
                continue
            key = parts[0].strip()
            remainder = parts[1].strip().split()
            if not remainder:
                continue
            with suppress(ValueError):
                values[key] = int(remainder[0]) * 1024

        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        if total is None:
            return None, None
        if available is None:
            available = values.get("MemFree")
        used = None
        if available is not None:
            used = max(total - available, 0)
        return total, used

    async def _get_user_install_count(self) -> int | None:
        try:
            info = await self.bot.application_info()
        except discord.HTTPException:
            info = None
        if info is not None:
            count = getattr(info, "approximate_user_install_count", None)
            if isinstance(count, int):
                return count
        return len(self.bot.users)

    @commands.command(name="stats", hidden=True)
    @commands.is_owner()
    async def stats(self, ctx: commands.Context) -> None:
        """
        Show bot resource usage.
        """
        total, used = self._read_meminfo()
        cache_entries = len(cache._store)
        guild_count = len(self.bot.guilds)
        user_installs = await self._get_user_install_count()

        try:
            load_1, load_5, load_15 = os.getloadavg()
            cpu_line = f"{load_1:.2f} / {load_5:.2f} / {load_15:.2f}"
        except (AttributeError, OSError):
            cpu_line = "Unknown"

        embed = discord.Embed(title="Owner Stats", color=discord.Color.blue())
        if used is not None and total is not None:
            embed.add_field(
                name="RAM",
                value=f"{self._format_bytes(used)} / {self._format_bytes(total)}",
                inline=False,
            )
        else:
            embed.add_field(name="RAM", value="Unknown", inline=False)
        embed.add_field(name="CPU Load", value=cpu_line, inline=False)
        embed.add_field(name="Cache Entries", value=f"{cache_entries:,}", inline=False)
        embed.add_field(name="Total Servers", value=f"{guild_count:,}", inline=False)
        embed.add_field(
            name="Total User Installs",
            value=f"{user_installs:,}" if user_installs is not None else "Unknown",
            inline=False,
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.command(name="restart", hidden=True)
    @commands.is_owner()
    async def restart(self, ctx: commands.Context) -> None:
        """
        Restart the bot (owner only).
        """
        confirmed = await confirm_action(
            ctx,
            "Restart the bot?",
            timeout=20,
            ephemeral=False,
            confirm_label="Restart",
            cancel_label="Cancel",
            confirm_message="Restarting...",
            cancel_message="Restart cancelled.",
            timeout_message="Restart cancelled.",
        )
        if not confirmed:
            return

        await self.bot.close()
        os.execv(sys.executable, [sys.executable, *sys.argv])

    @commands.command(name="export-commands", hidden=True)
    @commands.is_owner()
    async def export_commands_cmd(self, ctx: commands.Context, output: str | None = None) -> None:
        """
        Export commands metadata to a JSON file.
        """
        output_path = output or "docs/commands.json"
        try:
            export_commands(self.bot, output_path)
        except Exception as exc:
            await ctx.reply(
                f"Failed to export commands: {exc}",
                mention_author=False,
            )
            return
        await ctx.reply(
            f"Exported commands to `{output_path}`",
            mention_author=False,
        )


async def setup(bot: Amenity) -> None:
    await bot.add_cog(Owner(bot))
