import os
import sqlite3
import time as tm
from collections.abc import Iterable
from contextlib import suppress

import discord
from discord import app_commands
from discord.ext import commands, tasks

from api.buttons import confirm_action
from api.emojis import Emoji
from api.log import log_command_error
from api.paginator import EmbedPaginator, PaginatorHelper
from api.parser import StringToTime
from core.amenity import Amenity
from core.cache import cache


class Reminder(commands.Cog):
    display_name = "Reminders"
    group_name = "Reminders"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data/reminders.db"))
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()
        self.check_reminders.start()
        self.remind_me_about_menu = app_commands.ContextMenu(
            name="Remind me about it",
            callback=self.remind_me_about,
            type=discord.AppCommandType.message,
        )
        self.bot.tree.add_command(self.remind_me_about_menu)

    def cog_unload(self) -> None:
        self.check_reminders.cancel()
        self.bot.tree.remove_command(
            self.remind_me_about_menu.name,
            type=self.remind_me_about_menu.type,
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    remind_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS reminders_user_id ON reminders (user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS reminders_remind_at ON reminders (remind_at)")

    def _cache_key(self, user_id: int) -> str:
        return f"reminders:{user_id}"

    def _fetch_user_reminders(self, user_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, remind_at, created_at FROM reminders WHERE user_id = ? ORDER BY remind_at",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _get_user_reminders(self, user_id: int) -> list[dict]:
        return cache.get_or_set(
            self._cache_key(user_id),
            lambda: self._fetch_user_reminders(user_id),
            ttl=60,
        )

    def _invalidate_user_cache(self, user_id: int) -> None:
        cache.delete(self._cache_key(user_id))

    def _dedupe_reminder_name(self, user_id: int, name: str) -> str:
        reminders = self._get_user_reminders(user_id)
        existing = {reminder["name"] for reminder in reminders}
        if name not in existing:
            return name

        max_len = 120 - 5  # reserve space for suffix
        counter = 1
        while True:
            suffix = f" ({counter})"
            base_len = max_len - len(suffix)
            if base_len < 1:
                base_len = 1
            candidate = f"{name[:base_len]}{suffix}"
            if len(candidate) > max_len:
                candidate = candidate[:max_len]
            if candidate not in existing:
                return candidate
            counter += 1

    def _insert_reminder(
        self,
        user_id: int,
        name: str,
        remind_at: int,
        created_at: int | None = None,
    ) -> str:
        if created_at is None:
            created_at = int(tm.time())
        name = self._dedupe_reminder_name(user_id, name)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO reminders (user_id, name, remind_at, created_at) VALUES (?, ?, ?, ?)",
                (user_id, name, remind_at, created_at),
            )
        self._invalidate_user_cache(user_id)
        return name

    async def _send_due_reminders(self, rows: Iterable[sqlite3.Row]) -> None:
        for row in rows:
            user_id = int(row["user_id"])
            name = row["name"]
            reminder_id = row["id"]
            user = self.bot.get_user(user_id)
            if user is None:
                try:
                    user = await self.bot.fetch_user(user_id)
                except discord.HTTPException:
                    user = None
            if user is not None:
                with suppress(discord.HTTPException):
                    await user.send(
                        embed=discord.Embed(
                            title=f"{Emoji.TIME.value} Reminder",
                            description=f"Reminding you about:\n> {name}",
                            timestamp=discord.utils.utcnow(),
                        )
                    )
            with self._connect() as conn:
                conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
            self._invalidate_user_cache(user_id)

    @tasks.loop(seconds=30)
    async def check_reminders(self) -> None:
        now = int(tm.time())
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, user_id, name FROM reminders WHERE remind_at <= ? ORDER BY remind_at LIMIT 50",
                (now,),
            ).fetchall()
        if rows:
            await self._send_due_reminders(rows)

    @check_reminders.before_loop
    async def check_reminders_before_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _send_embed(
        self,
        ctx: commands.Context,
        description: str,
        title: str | None = None,
        ephemeral: bool = False,
    ) -> None:
        embed = discord.Embed(description=description)
        if title:
            embed.title = title
        if ctx.interaction:
            if ctx.interaction.response.is_done():
                await ctx.interaction.followup.send(embed=embed, ephemeral=ephemeral)
            else:
                await ctx.interaction.response.send_message(embed=embed, ephemeral=ephemeral)
            return
        await ctx.send(embed=embed)

    @commands.hybrid_group(name="reminder", description="Manage reminders", aliases=["re"])
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def reminder(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await self._send_embed(
                ctx,
                "Use `/reminder create`, `/reminder list`, `/reminder delete`, or `/reminder nuke`.",
                ephemeral=True,
            )
            return

    @reminder.command(name="create", description="Create a new reminder", aliases=["c"])
    @app_commands.describe(
        time="When to be reminded (e.g. 1h30m, 2:25am UTC, 14:25 UTC+5:45, <t:1778847300:t>)",
        name="The reminder message",
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.max_concurrency(10, commands.BucketType.default, wait=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def reminder_create(self, ctx: commands.Context, time: str, *, name: str) -> None:
        if not time:
            await self._send_embed(
                ctx,
                "Please specify a time for the reminder.",
                ephemeral=True,
            )
            return
        if not name:
            await self._send_embed(
                ctx,
                "Please specify a name for the reminder.",
                ephemeral=True,
            )
            return
        if len(name) > 120:
            await self._send_embed(
                ctx,
                "Reminder name is too long (max 120 characters).",
                ephemeral=True,
            )
            return

        try:
            sec = StringToTime(time)
            if sec < 30:
                await self._send_embed(
                    ctx,
                    "Please specify a time of at least 30 seconds.",
                    ephemeral=True,
                )
                return

            now = int(tm.time())
            remind_at = now + sec
            name = self._insert_reminder(
                ctx.author.id,
                name,
                remind_at,
                created_at=now,
            )

            await self._send_embed(
                ctx,
                f"You will be reminded about '{name}' in <t:{remind_at}:R>.",
                ephemeral=True,
            )
        except Exception as exc:
            await self._send_embed(ctx, "Error creating reminder", ephemeral=True)
            await log_command_error(ctx, exc)

    @reminder.command(name="list", description="List your reminders", aliases=["ls"])
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.max_concurrency(10, commands.BucketType.default, wait=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def reminder_list(self, ctx: commands.Context) -> None:
        reminders = self._get_user_reminders(ctx.author.id)
        if not reminders:
            await self._send_embed(ctx, "You have no reminders.", ephemeral=True)
            return

        lines = [f"{reminder['name']} | <t:{reminder['remind_at']}:R>" for reminder in reminders]
        embeds = PaginatorHelper.create_adaptive_embeds(
            lines,
            title=f"{Emoji.TIME.value} Your reminders",
            items_per_page=10,
            max_chars=1000,
        )

        if ctx.interaction:
            view = EmbedPaginator(embeds, author_id=ctx.author.id)
            await ctx.interaction.response.send_message(
                embed=embeds[0],
                view=view,
                ephemeral=True,
            )
            return

        view = EmbedPaginator(embeds, author_id=ctx.author.id)
        await ctx.send(embed=embeds[0], view=view)

    async def remind_me_about(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        modal = ReminderContextModal(self, message)
        await interaction.response.send_modal(modal)

    @reminder.command(name="delete", description="Delete a reminder", aliases=["del"])
    @app_commands.describe(name="The name of the reminder to delete")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.max_concurrency(10, commands.BucketType.default, wait=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def reminder_delete(self, ctx: commands.Context, name: str) -> None:
        name = name.strip()
        if not name:
            await self._send_embed(ctx, "Reminder name is required.", ephemeral=True)
            return

        if name.startswith("id:"):
            id_value = name[3:].strip()
            if not id_value.isdigit():
                await self._send_embed(ctx, "Reminder not found.", ephemeral=True)
                return
            reminder_id = int(id_value)
            with self._connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM reminders WHERE id = ? AND user_id = ?",
                    (reminder_id, ctx.author.id),
                )
        else:
            if len(name) > 120:
                await self._send_embed(
                    ctx,
                    "Reminder name is too long (max 120 characters).",
                    ephemeral=True,
                )
                return
            with self._connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM reminders WHERE name = ? AND user_id = ?",
                    (name, ctx.author.id),
                )
        if cursor.rowcount == 0:
            await self._send_embed(ctx, "Reminder not found.", ephemeral=True)
            return
        self._invalidate_user_cache(ctx.author.id)
        await self._send_embed(
            ctx,
            f"Deleted reminder named `{name}`.",
            ephemeral=True,
        )

    @reminder_delete.autocomplete("name")
    async def reminder_delete_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        reminders = self._get_user_reminders(interaction.user.id)
        current_lower = current.strip().lower()
        choices: list[app_commands.Choice[str]] = []
        for reminder in reminders:
            name = reminder["name"]
            if current_lower and current_lower not in name.lower():
                continue
            if len(name) > 100:
                display_name = f"{name[:97]}..."
                value = f"id:{reminder['id']}"
            else:
                display_name = name
                value = name
            choices.append(app_commands.Choice(name=display_name, value=value))
            if len(choices) >= 25:
                break
        return choices

    @reminder.command(name="nuke", description="Nuke all your reminders")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.max_concurrency(10, commands.BucketType.default, wait=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def reminder_nuke(self, ctx: commands.Context) -> None:
        confirmed = await confirm_action(
            ctx,
            "This will delete all of your reminders. Continue?",
            confirm_label="Delete",
            cancel_label="Cancel",
            confirm_style=discord.ButtonStyle.danger,
            cancel_style=discord.ButtonStyle.secondary,
            timeout=20.0,
            ephemeral=True,
            confirm_message="Deleting reminders...",
            cancel_message="Canceled.",
            timeout_message="Timed out.",
        )
        if not confirmed:
            return

        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM reminders WHERE user_id = ?",
                    (ctx.author.id,),
                )
            self._invalidate_user_cache(ctx.author.id)
            await self._send_embed(ctx, f"Deleted {cursor.rowcount} reminders.", ephemeral=True)
        except Exception as exc:
            await self._send_embed(ctx, "Error deleting reminders.", ephemeral=True)
            await log_command_error(ctx, exc)


async def setup(bot: Amenity) -> None:
    await bot.add_cog(Reminder(bot))


class ReminderContextModal(discord.ui.Modal):
    def __init__(self, reminder_cog: Reminder, message: discord.Message) -> None:
        super().__init__(title=f"{Emoji.TIME.value} Create reminder")
        self.reminder_cog = reminder_cog
        self.message = message
        self.time_input = discord.ui.TextInput(
            label="When should I remind you?",
            placeholder="e.g. 1h30m, 2:25am UTC, 14:25 UTC+5:45",
            max_length=50,
        )
        self.name_input = discord.ui.TextInput(
            label="Reminder name",
            placeholder="e.g. giveaway of ps5",
            max_length=120,
        )
        self.add_item(self.time_input)
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        time_value = self.time_input.value.strip()
        name_value = self.name_input.value.strip()

        if not time_value:
            await interaction.response.send_message(
                "Please specify a time for the reminder.",
                ephemeral=True,
            )
            return

        if not name_value:
            await interaction.response.send_message(
                "Please specify a name for the reminder.",
                ephemeral=True,
            )
            return

        try:
            sec = StringToTime(time_value)
        except Exception:
            await interaction.response.send_message(
                "Please specify a valid time for the reminder.",
                ephemeral=True,
            )
            return

        if sec < 30:
            await interaction.response.send_message(
                "Please specify a time of at least 30 seconds.",
                ephemeral=True,
            )
            return

        message_link = self.message.jump_url
        suffix = f" • {message_link}"
        max_len = 120
        if len(suffix) >= max_len:
            await interaction.response.send_message(
                "Message link is too long to store in a reminder.",
                ephemeral=True,
            )
            return

        base_max = max_len - len(suffix)
        if base_max < 1:
            base_max = 1
        trimmed_name = name_value[:base_max].rstrip()
        full_name = f"{trimmed_name}{suffix}"

        now = int(tm.time())
        remind_at = now + sec
        full_name = self.reminder_cog._insert_reminder(
            interaction.user.id,
            full_name,
            remind_at,
            created_at=now,
        )

        await interaction.response.send_message(
            f"You will be reminded about '{full_name}' in <t:{remind_at}:R>.",
            ephemeral=True,
        )
