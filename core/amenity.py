import asyncio
import faulthandler
import logging
import os
import pkgutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from api.log import (
    log_app_command_error,
    log_command_error,
    log_command_usage,
)
from core.checks import (
    CommandDisabled,
    PremiumRequired,
    UserBlacklisted,
    cleanup_expired_premium,
    command_enabled_predicate,
    initialize_checks,
    user_not_blacklisted_predicate,
)
from core.help import AmenityHelpCommand
from core.installed_users import (
    flush_installed_users as flush_pending_installed_users,
)
from core.installed_users import init_installed_users_db, track_installed_user

logger = logging.getLogger(__name__)

# The gateway reports a missed heartbeat only after the event loop has already
# stopped for ten seconds.  Start maintenance slightly later so a fresh gateway
# connection has time to settle, and use a watchdog thread to capture the
# *actual* Python stack if the loop ever stops making progress again.
STARTUP_MAINTENANCE_DELAY_SECONDS = 30
EVENT_LOOP_WATCHDOG_INTERVAL_SECONDS = 1
EVENT_LOOP_STALL_SECONDS = 10
# ``asyncio.to_thread`` normally grows its executor the first time a new worker
# is needed.  ``Thread.start()`` waits synchronously for that worker to boot,
# which is exactly where the gateway loop stalled in production.  A fixed,
# pre-warmed pool moves that work to setup, before the gateway is connected.
BLOCKING_WORKER_COUNT = 4

USER_ONLY_INSTALL_MESSAGE = (
    "Amenity is a user-only app and cannot be installed to servers. "
    "Please install it to your Discord account instead."
)

os.environ["JISHAKU_HIDE"] = "True"
os.environ["JISHAKU_NO_UNDERSCORE"] = "True"
os.environ["JISHAKU_FORCE_PAGINATOR"] = "True"


class Amenity(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True
        intents.messages = True
        intents.dm_messages = True
        super().__init__(
            command_prefix=",",
            intents=intents,
            case_insensitive=True,
            help_command=AmenityHelpCommand(),
            owner_id=931347423773741097,
            strip_after_prefix=True,
            allowed_mentions=discord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=True)
        )
        self._event_loop_last_tick = time.monotonic()
        self._event_loop_watchdog_stop = threading.Event()
        self._event_loop_watchdog_thread: threading.Thread | None = None
        self._event_loop_watchdog_task: asyncio.Task[None] | None = None
        self._blocking_executor = ThreadPoolExecutor(
            max_workers=BLOCKING_WORKER_COUNT,
            thread_name_prefix="amenity-worker",
        )
        self._blocking_executor_ready = False

    async def on_connect(self) -> None:
        """Called when bot connects to Discord gateway."""
        await self.change_presence(
            status=discord.Status.idle,
            activity=discord.Activity(type=discord.ActivityType.listening, name="/help"),
        )

    async def setup_hook(self) -> None:
        self.tree.on_error = self.on_app_command_error
        await self._prepare_blocking_executor()
        self._start_event_loop_watchdog()
        await initialize_checks()
        await asyncio.to_thread(init_installed_users_db)
        self.add_check(user_not_blacklisted_predicate)
        self.add_check(command_enabled_predicate)
        self.check_premium_expiry.start()
        self.flush_installed_users.start()
        failed_extensions: list[str] = []

        try:
            await self.load_extension("jishaku")
        except Exception as e:
            failed_extensions.append("jishaku")
            logger.exception("Failed to load extension jishaku: %s", e)

        try:
            await self.load_extension("core.help")
        except Exception as e:
            failed_extensions.append("core.help")
            logger.exception("Failed to load extension core.help: %s", e)

        cogs_path = Path(__file__).resolve().parents[1] / "cogs"
        for module in pkgutil.iter_modules([str(cogs_path)]):
            if module.ispkg:
                continue
            extension = f"cogs.{module.name}"
            try:
                await self.load_extension(extension)
                print(f"[+] {extension}")
            except Exception as e:
                failed_extensions.append(extension)
                logger.exception("[?] %s: %s", extension, e)

        if failed_extensions:
            failed = ", ".join(failed_extensions)
            raise RuntimeError(f"Failed to load required extension(s): {failed}")

        # guild_id: int = os.getenv("GUILD_ID")
        # if guild_id:
        #     guild = discord.Object(id=int(guild_id))
        #     self.tree.copy_global_to(guild=guild)
        #     await self.tree.sync(guild=guild)
        # else:
        await self.tree.sync()

    async def close(self) -> None:
        self.check_premium_expiry.cancel()
        self.flush_installed_users.cancel()
        await self._stop_event_loop_watchdog()
        await asyncio.to_thread(flush_pending_installed_users)
        self._blocking_executor.shutdown(wait=False, cancel_futures=True)
        await super().close()

    @staticmethod
    def _wait_for_worker_pool(barrier: threading.Barrier) -> None:
        barrier.wait(timeout=10)

    async def _prepare_blocking_executor(self) -> None:
        if self._blocking_executor_ready:
            return

        loop = asyncio.get_running_loop()
        loop.set_default_executor(self._blocking_executor)
        barrier = threading.Barrier(BLOCKING_WORKER_COUNT)
        try:
            await asyncio.gather(
                *(
                    loop.run_in_executor(None, self._wait_for_worker_pool, barrier)
                    for _ in range(BLOCKING_WORKER_COUNT)
                )
            )
        except threading.BrokenBarrierError as exc:
            raise RuntimeError("Could not start blocking workers before opening the Discord gateway.") from exc
        self._blocking_executor_ready = True

    def _start_event_loop_watchdog(self) -> None:
        if self._event_loop_watchdog_thread is not None:
            return

        self._event_loop_watchdog_stop.clear()
        self._event_loop_last_tick = time.monotonic()
        self._event_loop_watchdog_task = asyncio.create_task(
            self._pulse_event_loop_watchdog(),
            name="amenity-event-loop-watchdog-pulse",
        )
        self._event_loop_watchdog_thread = threading.Thread(
            target=self._watch_event_loop,
            name="amenity-event-loop-watchdog",
            daemon=True,
        )
        self._event_loop_watchdog_thread.start()

    async def _stop_event_loop_watchdog(self) -> None:
        self._event_loop_watchdog_stop.set()

        task = self._event_loop_watchdog_task
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self._event_loop_watchdog_task = None

        thread = self._event_loop_watchdog_thread
        if thread is not None:
            await asyncio.to_thread(thread.join, EVENT_LOOP_WATCHDOG_INTERVAL_SECONDS + 1)
            self._event_loop_watchdog_thread = None

    async def _pulse_event_loop_watchdog(self) -> None:
        try:
            while True:
                self._event_loop_last_tick = time.monotonic()
                await asyncio.sleep(EVENT_LOOP_WATCHDOG_INTERVAL_SECONDS)
        finally:
            self._event_loop_last_tick = time.monotonic()

    def _watch_event_loop(self) -> None:
        reported_stall = False
        while not self._event_loop_watchdog_stop.wait(EVENT_LOOP_WATCHDOG_INTERVAL_SECONDS):
            stalled_for = time.monotonic() - self._event_loop_last_tick
            if stalled_for < EVENT_LOOP_STALL_SECONDS:
                reported_stall = False
                continue
            if reported_stall:
                continue

            reported_stall = True
            logger.critical(
                "Event loop made no progress for %.1f seconds; dumping all Python thread stacks.",
                stalled_for,
            )
            try:
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            except OSError:
                logger.exception("Could not dump Python stacks for the stalled event loop.")

    async def _wait_for_startup_maintenance(self) -> None:
        await self.wait_until_ready()
        await asyncio.sleep(STARTUP_MAINTENANCE_DELAY_SECONDS)

    @tasks.loop(hours=1)
    async def check_premium_expiry(self) -> None:
        removed = await asyncio.to_thread(cleanup_expired_premium)
        if removed:
            logger.info("Removed %s expired premium subscription(s).", removed)

    @check_premium_expiry.before_loop
    async def before_check_premium_expiry(self) -> None:
        await self._wait_for_startup_maintenance()

    @tasks.loop(minutes=30)
    async def flush_installed_users(self) -> None:
        flushed = await asyncio.to_thread(flush_pending_installed_users)
        if flushed:
            logger.info("Flushed %s tracked command user(s).", flushed)

    @flush_installed_users.before_loop
    async def before_flush_installed_users(self) -> None:
        await self._wait_for_startup_maintenance()

    async def on_ready(self) -> None:
        # if not self.user:
        #     return
        # app_info = await self.application_info()
        # install_scope = (
        #     "users"
        #     if app_info.install_params and app_info.install_params.scopes
        #     else "unknown"
        # )
        # print(
        #     f"Logged in as {self.user} (ID: {self.user.id}) | install scope: {install_scope}"
        # )
        logger.info(f"[+] | LOGGED IN AS {self.user}")
        # logger.info(f"[+] | WATCHING {self.users}")

    async def _find_guild_inviter(self, guild: discord.Guild) -> discord.User | discord.Member | None:
        if self.user is None:
            return None

        try:
            async for entry in guild.audit_logs(
                limit=5,
                action=discord.AuditLogAction.bot_add,
            ):
                if entry.target and entry.target.id == self.user.id:
                    return entry.user
        except discord.Forbidden:
            logger.warning("Missing audit log permissions in guild %s (%s).", guild.name, guild.id)
        except discord.HTTPException as exc:
            logger.warning("Failed to fetch audit logs for guild %s (%s): %s", guild.name, guild.id, exc)

        return None

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await asyncio.sleep(2)

        inviter = await self._find_guild_inviter(guild)
        if inviter is not None and inviter.id == self.owner_id:
            logger.info("Allowed owner-installed guild %s (%s).", guild.name, guild.id)
            return

        if inviter is not None:
            try:
                await inviter.send(USER_ONLY_INSTALL_MESSAGE)
            except discord.Forbidden:
                logger.warning("Could not DM inviter %s after joining guild %s (%s).", inviter, guild.name, guild.id)
            except discord.HTTPException as exc:
                logger.warning(
                    "Failed to DM inviter %s after joining guild %s (%s): %s",
                    inviter,
                    guild.name,
                    guild.id,
                    exc,
                )
        else:
            logger.warning("Could not determine inviter for guild %s (%s).", guild.name, guild.id)

        try:
            await guild.leave()
            logger.info("Left guild %s (%s) because Amenity is user-only.", guild.name, guild.id)
        except discord.HTTPException as exc:
            logger.error("Failed to leave guild %s (%s): %s", guild.name, guild.id, exc)

    async def on_command_error(
        self,
        context: commands.Context,
        exception: Exception,
    ) -> None:
        if isinstance(exception, commands.CommandNotFound):
            return

        if isinstance(exception, commands.CommandOnCooldown):
            await context.reply(
                f"Command on cooldown. Try again after {exception.retry_after:.2f} seconds.",
                ephemeral=True,
                mention_author=False,
                delete_after=5,
            )
            return

        if isinstance(exception, commands.BadArgument):
            await context.send_help(context.command)
            return

        if isinstance(exception, commands.NoPrivateMessage):
            await context.reply(
                "This command can only be used in a server.",
                ephemeral=True,
                mention_author=False,
                delete_after=5,
            )
            return
        if isinstance(exception, commands.MissingRequiredArgument):
            await context.send_help(context.command)
            return

        if isinstance(exception, UserBlacklisted | CommandDisabled | PremiumRequired):
            await context.reply(
                str(exception),
                ephemeral=True,
                mention_author=False,
                delete_after=5,
            )
            return

        if isinstance(exception, commands.CheckFailure):
            await context.reply(
                "You don't have permission to use this command.",
                ephemeral=True,
                mention_author=False,
                delete_after=5,
            )
            return

        if isinstance(exception, commands.UserInputError):
            await context.send_help(context.command)
            return

        if isinstance(exception, commands.MaxConcurrencyReached):
            await context.reply(
                "This command is currently being used by too many people. Please try again later.",
                ephemeral=True,
                mention_author=False,
                delete_after=5,
            )
            return
        await log_command_error(context, exception)
        raise exception

    async def on_command_completion(self, context: commands.Context) -> None:
        track_installed_user(context.author)
        await log_command_usage(context)

    async def on_app_command_completion(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command,
    ) -> None:
        del command
        track_installed_user(interaction.user)

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        exception: app_commands.AppCommandError,
    ) -> None:
        async def send_error(message: str) -> None:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                    return
                await interaction.response.send_message(message, ephemeral=True)
            except discord.HTTPException:
                return

        original = getattr(exception, "original", exception)

        if isinstance(exception, app_commands.CommandOnCooldown) or isinstance(original, commands.CommandOnCooldown):
            retry_after = getattr(exception, "retry_after", getattr(original, "retry_after", 0.0))
            await send_error(f"Command on cooldown. Try again after {retry_after:.2f} seconds.")
            return

        if isinstance(exception, app_commands.TransformerError | app_commands.CommandSignatureMismatch):
            await send_error("Invalid argument provided. Please check your input.")
            return

        if isinstance(exception, app_commands.NoPrivateMessage) or isinstance(original, commands.NoPrivateMessage):
            await send_error("This command can only be used in a server.")
            return

        if isinstance(original, UserBlacklisted | CommandDisabled | PremiumRequired):
            await send_error(str(original))
            return

        if isinstance(exception, app_commands.CheckFailure) or isinstance(original, commands.CheckFailure):
            await send_error("You don't have permission to use this command.")
            return

        await send_error("An unexpected error occurred. Please try again later.")
        await log_app_command_error(interaction, exception)
        raise exception

    # async def invoke_help_command(self, ctx: commands.Context) -> None:
    #     """Send help for the current command."""
    #     await ctx.send_help(ctx.command)
