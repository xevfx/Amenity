import asyncio
import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import discord
from discord.ext import commands

from api.buttons import confirm_action
from api.commands_export import export_commands
from api.paginator import EmbedPaginator, PaginatorHelper
from api.users import fetch_user_cached
from core.amenity import Amenity
from core.cache import cache
from core.checks import (
    add_premium,
    blacklist_user,
    disable_command,
    enable_command,
    generate_premium_keys,
    get_premium_expires_at,
    is_command_disabled,
    is_user_blacklisted,
    list_premium_keys,
    parse_duration,
    remove_premium,
    revoke_premium,
    revoke_premium_key,
    unblacklist_user,
)
from core.installed_users import InstalledUser, list_installed_users

BROADCAST_HEADER = "**Amenity announcement**\n\n"
BROADCAST_FOOTER = "\n\n-# You are receiving this because you have used Amenity."
MAX_BROADCAST_BODY_LENGTH = 2000 - len(BROADCAST_HEADER) - len(BROADCAST_FOOTER)
BROADCAST_DELAY_SECONDS = 0.4
BROADCAST_PROGRESS_INTERVAL = 25


@dataclass(slots=True)
class BroadcastResult:
    total: int
    sent: int = 0
    blocked: int = 0
    unavailable: int = 0
    failed: int = 0

    @property
    def processed(self) -> int:
        return self.sent + self.blocked + self.unavailable + self.failed


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

    def _read_int(self, path: Path) -> int | None:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not value or value == "max":
            return None
        with suppress(ValueError):
            return int(value)
        return None

    def _parse_id(self, value: str) -> int:
        value = value.strip()
        if value.startswith("<@") and value.endswith(">"):
            value = value.removeprefix("<@").removeprefix("!").removesuffix(">")
        try:
            return int(value)
        except ValueError as exc:
            raise commands.BadArgument("Provide a valid user ID.") from exc

    def _find_command_name(self, name: str) -> str | None:
        normalized = " ".join(name.lower().strip().split())
        if not normalized:
            return None
        command = self.bot.get_command(normalized)
        if command is None:
            return normalized
        return command.qualified_name.lower()

    async def _send_owner_reply(self, ctx: commands.Context, message: str) -> None:
        await ctx.reply(message, mention_author=False)

    def _validate_duration(self, duration: str) -> str:
        try:
            parse_duration(duration)
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
        return duration.lower()

    def _format_premium_status(self, expires_at: int | None) -> str:
        if expires_at is None:
            return "No active premium."
        return f"Premium expires <t:{expires_at}:R> (`{expires_at}`)."

    def _format_installed_user(self, user: InstalledUser, index: int) -> str:
        label = f"<@{user.user_id}>"
        username = user.username or "unknown"
        display_name = user.display_name or username
        return (
            f"`{index}.` {label} (`{user.user_id}`)\n"
            f"Name: `{display_name}` / `{username}` | Commands: `{user.command_count}` | Last: <t:{user.last_seen}:R>"
        )

    def _format_installed_users(self, users: list[InstalledUser]) -> list[str]:
        return [self._format_installed_user(user, index) for index, user in enumerate(users, start=1)]

    def _format_broadcast_status(self, result: BroadcastResult, *, complete: bool = False) -> str:
        heading = "Broadcast complete." if complete else "Broadcast in progress..."
        return (
            f"{heading}\n"
            f"Processed: `{result.processed}/{result.total}`\n"
            f"Sent: `{result.sent}` | DMs closed: `{result.blocked}` | "
            f"Unavailable: `{result.unavailable}` | Failed: `{result.failed}`"
        )

    async def _broadcast_tracked_user_dm(
        self,
        users: list[InstalledUser],
        payload: str,
        *,
        progress_message: discord.Message | None = None,
        delay: float = BROADCAST_DELAY_SECONDS,
    ) -> BroadcastResult:
        result = BroadcastResult(total=len(users))

        for index, tracked_user in enumerate(users, start=1):
            user = self.bot.get_user(tracked_user.user_id)
            if user is None:
                try:
                    user = await fetch_user_cached(self.bot, tracked_user.user_id)
                except discord.NotFound:
                    result.unavailable += 1
                except discord.HTTPException:
                    result.failed += 1

            if user is not None:
                try:
                    await user.send(
                        payload,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.Forbidden:
                    result.blocked += 1
                except discord.NotFound:
                    result.unavailable += 1
                except discord.HTTPException:
                    result.failed += 1
                else:
                    result.sent += 1

            if progress_message is not None and (
                index % BROADCAST_PROGRESS_INTERVAL == 0 or index == result.total
            ):
                with suppress(discord.HTTPException):
                    await progress_message.edit(content=self._format_broadcast_status(result))

            if delay > 0 and index < result.total:
                await asyncio.sleep(delay)

        return result

    async def _send_generated_premium_keys(
        self,
        ctx: commands.Context,
        premium_duration: str,
        key_lifespan: str,
        count: int = 1,
    ) -> None:
        premium_duration = self._validate_duration(premium_duration)
        key_lifespan = self._validate_duration(key_lifespan)
        keys = await asyncio.to_thread(generate_premium_keys, premium_duration, key_lifespan, count)
        formatted = "\n".join(key.key for key in keys)
        await self._send_owner_reply(
            ctx,
            (
                f"Generated `{len(keys)}` premium key(s).\n"
                f"Premium duration: `{premium_duration}`\n"
                f"Key lifespan: `{key_lifespan}`\n"
                f"Use before: <t:{keys[0].key_expires_at}:R>\n"
                f"```text\n{formatted}\n```"
            ),
        )

    async def _revoke_premium_keys(self, ctx: commands.Context, keys: tuple[str, ...]) -> None:
        if not keys:
            await self._send_owner_reply(ctx, "Provide at least one premium key.")
            return

        results = await asyncio.to_thread(lambda: [revoke_premium_key(key) for key in keys])
        await self._send_owner_reply(ctx, f"Revoked `{sum(results)}` of `{len(keys)}` premium key(s).")

    def _get_cgroup_path(self, controller: str | None) -> Path | None:
        try:
            lines = Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
        except OSError:
            return None

        for line in lines:
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            _, controllers, rel_path = parts
            if controller is None and controllers == "":
                return Path("/sys/fs/cgroup") / rel_path.lstrip("/")
            if controller and controller in controllers.split(","):
                return Path("/sys/fs/cgroup") / controller / rel_path.lstrip("/")
        return None

    def _read_cgroup_memory(self) -> tuple[int | None, int | None]:
        cgroup_v2 = self._get_cgroup_path(None)
        if cgroup_v2 is not None:
            limit = self._read_int(cgroup_v2 / "memory.max")
            used = self._read_int(cgroup_v2 / "memory.current")
            if limit is not None and limit < (1 << 60) and used is not None:
                return limit, used

        cgroup_v1 = self._get_cgroup_path("memory")
        if cgroup_v1 is not None:
            limit = self._read_int(cgroup_v1 / "memory.limit_in_bytes")
            used = self._read_int(cgroup_v1 / "memory.usage_in_bytes")
            if limit is not None and limit < (1 << 60) and used is not None:
                return limit, used

        return None, None

    def _read_meminfo(self) -> tuple[int | None, int | None]:
        total, used = self._read_cgroup_memory()
        if total is not None and used is not None:
            return total, used

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
        total, used = await asyncio.to_thread(self._read_meminfo)
        cache_entries = len(cache)
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

    @commands.command(name="users", hidden=True)
    @commands.is_owner()
    async def users(self, ctx: commands.Context) -> None:
        """
        Show users who have used bot commands.
        """
        installed_users = await asyncio.to_thread(list_installed_users)
        if not installed_users:
            await self._send_owner_reply(ctx, "No command users have been tracked yet.")
            return

        lines = self._format_installed_users(installed_users)
        embeds = PaginatorHelper.create_adaptive_embeds(
            lines,
            "Tracked Command Users",
            items_per_page=8,
            max_chars=3500,
            color=discord.Color.blue(),
        )
        view = EmbedPaginator(embeds, author_id=ctx.author.id)
        await ctx.reply(embed=embeds[0], view=view, mention_author=False)

    @commands.command(
        name="dm-users",
        aliases=["broadcast-dm", "announce-users"],
        hidden=True,
    )
    @commands.is_owner()
    @commands.max_concurrency(1, commands.BucketType.default, wait=False)
    async def dm_users(self, ctx: commands.Context, *, message: str) -> None:
        """Send a confirmed operational announcement to every tracked user."""
        message = message.strip()
        if not message:
            await self._send_owner_reply(ctx, "Provide a message to broadcast.")
            return
        if len(message) > MAX_BROADCAST_BODY_LENGTH:
            await self._send_owner_reply(
                ctx,
                f"Message is too long. Keep it under `{MAX_BROADCAST_BODY_LENGTH}` characters.",
            )
            return

        installed_users = await asyncio.to_thread(list_installed_users)
        if not installed_users:
            await self._send_owner_reply(ctx, "No tracked users are available.")
            return

        preview = discord.utils.escape_mentions(message[:500])
        if len(message) > 500:
            preview = f"{preview}..."
        confirmed = await confirm_action(
            ctx,
            (
                f"Send this DM to `{len(installed_users)}` tracked user(s)?\n"
                "This cannot be undone.\n\n"
                f">>> {preview}"
            ),
            timeout=30,
            ephemeral=False,
            confirm_label="Send DMs",
            cancel_label="Cancel",
            confirm_style=discord.ButtonStyle.danger,
            confirm_message="Broadcast confirmed. Starting delivery...",
            cancel_message="Broadcast cancelled.",
            timeout_message="Broadcast cancelled.",
        )
        if not confirmed:
            return

        payload = f"{BROADCAST_HEADER}{message}{BROADCAST_FOOTER}"
        progress_message = await ctx.reply(
            f"Starting broadcast to `{len(installed_users)}` tracked user(s)...",
            mention_author=False,
        )
        result = await self._broadcast_tracked_user_dm(
            installed_users,
            payload,
            progress_message=progress_message,
        )
        final_status = self._format_broadcast_status(result, complete=True)
        try:
            await progress_message.edit(content=final_status)
        except discord.HTTPException:
            await self._send_owner_reply(ctx, final_status)

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
            await asyncio.to_thread(export_commands, self.bot, output_path)
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

    @commands.group(name="blacklist", invoke_without_command=True, hidden=True, aliases=["bl"])
    @commands.is_owner()
    async def blacklist(self, ctx: commands.Context) -> None:
        """
        Manage blacklisted users.
        """
        await ctx.send_help(ctx.command)

    @blacklist.command(name="add", hidden=True, aliases=["a"])
    @commands.is_owner()
    async def blacklist_add(self, ctx: commands.Context, user_id: str, *, reason: str | None = None) -> None:
        user_id_int = self._parse_id(user_id)
        await asyncio.to_thread(blacklist_user, user_id_int, reason)
        await self._send_owner_reply(ctx, f"Blacklisted `{user_id_int}`.")

    @blacklist.command(name="remove", aliases=["r"], hidden=True)
    @commands.is_owner()
    async def blacklist_remove(self, ctx: commands.Context, user_id: str) -> None:
        user_id_int = self._parse_id(user_id)
        removed = await asyncio.to_thread(unblacklist_user, user_id_int)
        status = "Removed" if removed else "Not found"
        await self._send_owner_reply(ctx, f"{status}: `{user_id_int}`.")

    @blacklist.command(name="check", aliases=["c"], hidden=True)
    @commands.is_owner()
    async def blacklist_check(self, ctx: commands.Context, user_id: str) -> None:
        user_id_int = self._parse_id(user_id)
        status = "blacklisted" if is_user_blacklisted(user_id_int) else "not blacklisted"
        await self._send_owner_reply(ctx, f"`{user_id_int}` is {status}.")

    @commands.group(name="command", invoke_without_command=True, hidden=True, aliases=["cmd"])
    @commands.is_owner()
    async def command_control(self, ctx: commands.Context) -> None:
        """
        Enable or disable commands.
        """
        await ctx.send_help(ctx.command)

    @command_control.command(name="disable", hidden=True)
    @commands.is_owner()
    async def command_disable(self, ctx: commands.Context, *, command_name: str) -> None:
        normalized = self._find_command_name(command_name)
        if normalized is None:
            await self._send_owner_reply(ctx, "Provide a command name.")
            return
        if normalized in {"command", "command disable", "command enable", "blacklist", "premium"}:
            await self._send_owner_reply(ctx, "That owner management command cannot be disabled.")
            return
        disabled = await asyncio.to_thread(disable_command, normalized)
        await self._send_owner_reply(ctx, f"Disabled `{disabled}`.")

    @command_control.command(name="enable", hidden=True)
    @commands.is_owner()
    async def command_enable(self, ctx: commands.Context, *, command_name: str) -> None:
        normalized = self._find_command_name(command_name)
        if normalized is None:
            await self._send_owner_reply(ctx, "Provide a command name.")
            return
        enabled = await asyncio.to_thread(enable_command, normalized)
        status = "Enabled" if enabled else "Not disabled"
        await self._send_owner_reply(ctx, f"{status}: `{normalized}`.")

    @command_control.command(name="check", hidden=True)
    @commands.is_owner()
    async def command_check(self, ctx: commands.Context, *, command_name: str) -> None:
        normalized = self._find_command_name(command_name)
        if normalized is None:
            await self._send_owner_reply(ctx, "Provide a command name.")
            return
        status = "disabled" if is_command_disabled(normalized) else "enabled"
        await self._send_owner_reply(ctx, f"`{normalized}` is {status}.")

    @commands.group(name="premium", invoke_without_command=True, hidden=True)
    @commands.is_owner()
    async def premium(self, ctx: commands.Context) -> None:
        """
        Manage premium expiry and keys.
        """
        await ctx.send_help(ctx.command)

    @premium.command(name="add", hidden=True)
    @commands.is_owner()
    async def premium_add(self, ctx: commands.Context, user_id: str, duration: str) -> None:
        user_id_int = self._parse_id(user_id)
        duration = self._validate_duration(duration)
        expires_at = await asyncio.to_thread(add_premium, user_id_int, duration)
        await self._send_owner_reply(ctx, f"Added `{duration}` premium to `{user_id_int}`. Expires <t:{expires_at}:R>.")

    @premium.command(name="remove", aliases=["deduct"], hidden=True)
    @commands.is_owner()
    async def premium_remove(self, ctx: commands.Context, user_id: str, duration: str) -> None:
        user_id_int = self._parse_id(user_id)
        duration = self._validate_duration(duration)
        expires_at = await asyncio.to_thread(remove_premium, user_id_int, duration)
        await self._send_owner_reply(
            ctx,
            f"Removed `{duration}` premium from `{user_id_int}`. {self._format_premium_status(expires_at)}",
        )

    @premium.command(name="revoke", hidden=True)
    @commands.is_owner()
    async def premium_revoke(self, ctx: commands.Context, user_id: str) -> None:
        user_id_int = self._parse_id(user_id)
        revoked = await asyncio.to_thread(revoke_premium, user_id_int)
        status = "Revoked" if revoked else "No premium found"
        await self._send_owner_reply(ctx, f"{status}: `{user_id_int}`.")

    @premium.command(name="check", hidden=True)
    @commands.is_owner()
    async def premium_check(self, ctx: commands.Context, user_id: str) -> None:
        user_id_int = self._parse_id(user_id)
        expires_at = get_premium_expires_at(user_id_int)
        await self._send_owner_reply(ctx, f"`{user_id_int}`: {self._format_premium_status(expires_at)}")

    @premium.command(name="generate", aliases=["generate-key", "genkey"], hidden=True)
    @commands.is_owner()
    async def premium_generate(
        self,
        ctx: commands.Context,
        premium_duration: str,
        key_lifespan: str,
        count: int = 1,
    ) -> None:
        await self._send_generated_premium_keys(ctx, premium_duration, key_lifespan, count)

    @premium.command(name="revoke-key", aliases=["revokekey", "revoke-keys", "revokekeys"], hidden=True)
    @commands.is_owner()
    async def premium_revoke_key(self, ctx: commands.Context, *keys: str) -> None:
        await self._revoke_premium_keys(ctx, keys)

    @premium.group(name="keys", invoke_without_command=True, hidden=True)
    @commands.is_owner()
    async def premium_keys(self, ctx: commands.Context) -> None:
        """
        Manage premium keys.
        """
        await ctx.send_help(ctx.command)

    @premium_keys.command(name="generate", aliases=["gen"], hidden=True)
    @commands.is_owner()
    async def premium_keys_generate(
        self,
        ctx: commands.Context,
        premium_duration: str,
        key_lifespan: str,
        count: int = 1,
    ) -> None:
        await self._send_generated_premium_keys(ctx, premium_duration, key_lifespan, count)

    @premium_keys.command(name="revoke", aliases=["remove"], hidden=True)
    @commands.is_owner()
    async def premium_keys_revoke(self, ctx: commands.Context, *keys: str) -> None:
        await self._revoke_premium_keys(ctx, keys)

    @premium_keys.command(name="list", aliases=["ls"], hidden=True)
    @commands.is_owner()
    async def premium_keys_list(self, ctx: commands.Context, limit: int = 10) -> None:
        keys = await asyncio.to_thread(list_premium_keys, limit)
        if not keys:
            await self._send_owner_reply(ctx, "No premium keys found.")
            return

        lines = []
        for key in keys:
            if key.revoked_at is not None:
                status = "revoked"
            elif key.used_at is not None:
                status = f"used by `{key.used_by}`"
            else:
                status = "active"
            lines.append(
                f"`{key.key}` | {status} | premium `{key.premium_duration}s` | use before <t:{key.key_expires_at}:R>"
            )
        await self._send_owner_reply(ctx, "\n".join(lines))


async def setup(bot: Amenity) -> None:
    await bot.add_cog(Owner(bot))
