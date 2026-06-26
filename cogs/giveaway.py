import contextlib
import datetime
import json
import random
import sqlite3

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from api.emojis import Emoji
from core.cache import cache

_ACTIVE_GW_KEY = "giveaway:active_list"
_GW_TTL = 10  # seconds

# Database setup
connection = sqlite3.connect("data/giveaways.db")
cursor = connection.cursor()
cursor.execute("""CREATE TABLE IF NOT EXISTS Giveaway (
                    guild_id INTEGER,
                    host_id INTEGER,
                    start_time TIMESTAMP,
                    ends_at TIMESTAMP,
                    prize TEXT,
                    winners INTEGER,
                    message_id INTEGER,
                    channel_id INTEGER,
                    participants_json TEXT,
                    PRIMARY KEY (message_id)
                )""")
connection.commit()
connection.close()


class GiveawayJoinView(discord.ui.View):
    """Persistent view for users to click and join a giveaway."""

    def __init__(self, bot: commands.Bot, cog: "UserAppGiveaway") -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.cog = cog
        self.participants: dict[int, set[int]] = {}

    # ── helpers that reach through to the cog's own connection ──────

    async def _get_ends_at(self, message_id: int) -> float | None:
        """Return ends_at timestamp, or None if not found."""
        row = await self.cog._fetch_giveaway(message_id, "ends_at")
        if row:
            return float(row[0])
        return None

    async def _persist_participants(self, message_id: int, pool: set[int]) -> None:
        """Write participant list into the DB using the cog's connection."""
        await self.cog.connection.execute(
            "UPDATE Giveaway SET participants_json = ? WHERE message_id = ?",
            (json.dumps(list(pool)), message_id),
        )
        await self.cog.connection.commit()

    async def _set_ended(self, message_id: int, pool: set[int]) -> None:
        """Mark giveaway as ended and save final participant snapshot."""
        await self.cog.connection.execute(
            "UPDATE Giveaway SET participants_json = ?, ends_at = 0 WHERE message_id = ?",
            (json.dumps(list(pool)), message_id),
        )
        await self.cog.connection.commit()

    # ── button ─────────────────────────────────────────────────────

    @discord.ui.button(label="0", style=discord.ButtonStyle.primary,
                       custom_id="join_giveaway_btn", emoji=Emoji.TADA.value)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        msg_id = interaction.message.id
        user_id = interaction.user.id

        # Check expiry early – especially important for user-install where
        # the background loop can't reach guild channels.
        try:
            ends_at = await self._get_ends_at(msg_id)
            if ends_at is not None and datetime.datetime.now().timestamp() >= round(ends_at):
                await interaction.response.defer(ephemeral=True)
                await self._end_from_interaction(interaction, msg_id)
                return
        except Exception:
            pass

        if msg_id not in self.participants:
            self.participants[msg_id] = set()

        if user_id in self.participants[msg_id]:
            self.participants[msg_id].remove(user_id)
            action_text = f"{Emoji.LIKE.value} You left the giveaway."
        else:
            self.participants[msg_id].add(user_id)
            action_text = f"{Emoji.TADA.value} You entered the giveaway!"

        # Persist participants immediately so they survive restarts.
        try:
            await self._persist_participants(msg_id, self.participants[msg_id])
        except Exception:
            pass

        # Update button label.
        button.label = str(len(self.participants[msg_id]))

        # Edit the original message via the interaction webhook (always works for
        # user-install – no guild membership needed) then send the ephemeral
        # follow-up.
        await interaction.response.edit_message(
            content=interaction.message.content,
            embeds=interaction.message.embeds,
            view=self,
        )
        await interaction.followup.send(action_text, ephemeral=True)

    # ── interaction-based giveaway end (user-install fallback) ──────

    async def _end_from_interaction(self, interaction: discord.Interaction, message_id: int) -> None:
        """End a giveaway inline when a user clicks after expiry."""
        try:
            row = await self.cog._fetch_giveaway(
                message_id, "guild_id, host_id, start_time, ends_at, prize, winners, channel_id",
            )
        except Exception:
            return
        if not row:
            return
        guild_id, host_id, _, _, prize, winners_count, channel_id = row
        current_time = int(datetime.datetime.now().timestamp())

        pool = list(self.participants.get(message_id, set()))

        if not pool:
            desc = (
                f"{Emoji.WARNING.value} This giveaway ended with no participants. 😕\n"
                f"Hosted by <@{int(host_id)}>"
            )
            embed = discord.Embed(description=desc, color=0x2F3136)
            embed.set_author(name=prize)
            embed.timestamp = discord.utils.utcnow()
            embed.set_footer(text="Ended at")
            # Best-effort edit of the original message via REST API.
            with contextlib.suppress(discord.HTTPException):
                await interaction.message.edit(
                    content=f"{Emoji.GIVEAWAY.value} **GIVEAWAY ENDED** {Emoji.GIVEAWAY.value}",
                    embed=embed, view=None,
                )
            # Acknowledge via the interaction webhook (always works).
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    f"{Emoji.GIVEAWAY.value} Giveaway ended with no participants.",
                    ephemeral=False,
                )
        else:
            actual_winners = min(len(pool), int(winners_count))
            selected = random.sample(pool, k=actual_winners)
            winner_mentions = ", ".join(f"<@!{uid}>" for uid in selected)

            desc = f"Ended <t:{int(current_time)}:R>\nHosted by <@{int(host_id)}>\nWinner(s): {winner_mentions}"
            embed = discord.Embed(description=desc, color=0x2F3136)
            embed.timestamp = discord.utils.utcnow()
            embed.set_author(name=prize)
            embed.set_footer(text="Ended at")

            with contextlib.suppress(discord.HTTPException):
                await interaction.message.edit(
                    content=(
                        f"{Emoji.GIVEAWAY.value} **GIVEAWAY ENDED** {Emoji.GIVEAWAY.value}\n"
                        f"> {Emoji.TADA.value} Congratulations! {winner_mentions} You won **{prize}**!"
                    ),
                    embed=embed, view=None,
                )
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    f"{winner_mentions} {Emoji.TADA.value} Congratulations! You won **{prize}**!",
                    ephemeral=False,
                )

        with contextlib.suppress(Exception):
            await self._set_ended(message_id, set(pool))

        self.participants.pop(message_id, None)
        self.cog._invalidate(message_id)


class UserAppGiveaway(commands.Cog):
    display_name = "Giveaway"
    group_name = "Utilities"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.join_view = GiveawayJoinView(bot, self)

    async def cog_load(self) -> None:
        self.connection = await aiosqlite.connect("data/giveaways.db")
        self.bot.add_view(self.join_view)  # Makes the button view persistent across bot reboots
        # Restore in-memory participants from database for active giveaways
        await self._restore_participants()
        self.GiveawayEnd.start()

    async def _restore_participants(self) -> None:
        """Load participants for active giveaways from DB into memory."""
        cursor = await self.connection.execute(
            "SELECT message_id, participants_json FROM Giveaway WHERE ends_at > 0"
        )
        rows = await cursor.fetchall()
        for message_id, participants_json in rows:
            try:
                pool = json.loads(participants_json)
                self.join_view.participants[int(message_id)] = set(pool)
            except (json.JSONDecodeError, TypeError):
                self.join_view.participants[int(message_id)] = set()

    async def cog_unload(self) -> None:
        await self.connection.close()
        self.GiveawayEnd.cancel()
        cache.delete(_ACTIVE_GW_KEY)

    # ── cache helpers ──────────────────────────────────────────────

    def _gw_key(self, message_id: int) -> str:
        return f"giveaway:{message_id}"

    def _invalidate(self, message_id: int | None = None) -> None:
        """Drop cached active list and, optionally, a single giveaway."""
        cache.delete(_ACTIVE_GW_KEY)
        if message_id is not None:
            cache.delete(self._gw_key(message_id))

    async def _fetch_active_giveaways(self) -> list[tuple]:
        """Return active giveaways, cached for the loop interval."""
        cached = cache.get(_ACTIVE_GW_KEY)
        if cached is not None:
            return cached
        cursor = await self.connection.execute(
            "SELECT ends_at, guild_id, message_id, host_id, "
            "winners, prize, channel_id "
            "FROM Giveaway WHERE ends_at > 0"
        )
        rows = await cursor.fetchall()
        cache.set(_ACTIVE_GW_KEY, rows, ttl=_GW_TTL)
        return rows

    async def _fetch_giveaway(self, message_id: int, columns: str) -> tuple | None:
        """Fetch a single giveaway row by message_id, with short caching."""
        key = self._gw_key(message_id)
        cached = cache.get(key)
        if cached is not None:
            return cached
        cursor = await self.connection.execute(
            f"SELECT {columns} FROM Giveaway WHERE message_id = ?",
            (message_id,),
        )
        row = await cursor.fetchone()
        if row is not None:
            cache.set(key, row, ttl=_GW_TTL)
        return row

    async def _count_running(self, guild_id: int | None) -> int:
        """Count active giveaways for a guild (or NULL scope), cached."""
        count_key = f"giveaway:count:{guild_id}"
        cached = cache.get(count_key)
        if cached is not None:
            return cached
        cursor = await self.connection.execute(
            "SELECT COUNT(*) FROM Giveaway WHERE guild_id IS ? AND ends_at > 0",
            (guild_id,),
        )
        (count,) = await cursor.fetchone()
        cache.set(count_key, count, ttl=_GW_TTL)
        return count

    def convert_time(self, time_str: str) -> int:
        pos = ["s", "m", "h", "d"]
        time_dict = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        unit = time_str[-1]
        if unit not in pos:
            return -1
        try:
            val = int(time_str[:-1])
        except ValueError:
            return -2
        return val * time_dict[unit]

    @commands.hybrid_command(name="gstart", description="Starts a new button-based giveaway.")
    @app_commands.describe(time="Duration (e.g., 10m, 5h, 1d)", winners="Number of winners", prize="Item up for grabs")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def gstart(self, ctx: commands.Context, time: str, winners: int, *, prize: str) -> None:

        if winners > 50 or winners <= 0:
            await ctx.send(f"{Emoji.WARNING.value} Winners must be between 1 and 50.", ephemeral=True)
            return

        converted = self.convert_time(time)
        if converted == -1 or converted == -2:
            await ctx.send(
                f"{Emoji.WARNING.value} Invalid time format. "
                "Use variations like: `10m`, `5h`, `1d`.",
                ephemeral=True,
            )
            return
        if converted > 2678400:  # 31 days max
            await ctx.send(f"{Emoji.WARNING.value} Time cannot exceed 31 days!", ephemeral=True)
            return

        guild_id = ctx.guild.id if ctx.guild else None

        # Limit running giveaways per scope
        running = await self._count_running(guild_id)
        if running >= 10:
            await ctx.send(
                f"{Emoji.WARNING.value} The maximum limit of active giveaways has been reached.",
                ephemeral=True,
            )
            return

        ends_timestamp = datetime.datetime.now().timestamp() + converted
        ends_utc = datetime.datetime.fromtimestamp(ends_timestamp, tz=datetime.UTC)

        desc = (
            f"{Emoji.LEAF.value} Winner(s): **{winners}**\n"
            f"{Emoji.TIME.value} Ends <t:{round(ends_timestamp)}:R> (<t:{round(ends_timestamp)}:f>)\n"
            f"Click the {Emoji.TADA.value} button below to participate!\n\n"
            f" {Emoji.SHINE.value} Hosted by {ctx.author.mention}"
        )
        embed = discord.Embed(
            description=desc,
            color=0x2F3136,
        )
        embed.timestamp = ends_utc
        embed.set_author(name=prize)
        embed.set_footer(text="Ends at")

        self.join_view.children[0].label = "0"

        gw_header = f"{Emoji.GIVEAWAY.value} **GIVEAWAY** {Emoji.GIVEAWAY.value}"
        message = await ctx.send(gw_header, embed=embed, view=self.join_view)

        self.join_view.participants[message.id] = set()

        query = (
            "INSERT INTO Giveaway(guild_id, host_id, start_time, ends_at, "
            "prize, winners, message_id, channel_id, participants_json) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        await self.connection.execute(
            query,
            (
                guild_id,
                ctx.author.id,
                datetime.datetime.now(),
                ends_timestamp,
                prize,
                winners,
                message.id,
                ctx.channel.id,
                "[]",
            ),
        )
        await self.connection.commit()
        self._invalidate(message.id)

    @tasks.loop(seconds=5)
    async def GiveawayEnd(self) -> None:
        try:
            await self._giveaway_end_pass()
        except Exception:
            pass

    async def _giveaway_end_pass(self) -> None:
        """One iteration of the giveaway end loop."""
        all_giveaways = await self._fetch_active_giveaways()
        current_time = datetime.datetime.now().timestamp()

        for giveaway in all_giveaways:
            ends_at, guild_id, message_id, host_id, winners_count, prize, channel_id = giveaway

            if int(current_time) >= round(float(ends_at)):
                ch = discord.PartialMessageable(state=self.bot._connection, id=int(channel_id))
                msg = ch.get_partial_message(int(message_id))

                pool = list(self.join_view.participants.get(int(message_id), set()))
                participants_json = json.dumps(pool)

                if len(pool) < 1:
                    desc = (
                        f"{Emoji.WARNING.value} This giveaway ended with no participants. 😕\n"
                        f"Hosted by <@{int(host_id)}>"
                    )
                    embed = discord.Embed(description=desc, color=0x2F3136)
                    embed.set_author(name=prize)
                    embed.timestamp = discord.utils.utcnow()
                    embed.set_footer(text="Ended at")

                    with contextlib.suppress(discord.HTTPException):
                        await msg.edit(
                            content=f"{Emoji.GIVEAWAY.value} **GIVEAWAY ENDED** {Emoji.GIVEAWAY.value}",
                            embed=embed,
                            view=None,
                        )

                    await self.connection.execute("DELETE FROM Giveaway WHERE message_id = ?", (int(message_id),))
                    self._invalidate(int(message_id))
                    self.join_view.participants.pop(int(message_id), None)
                    continue

                actual_winners = min(len(pool), int(winners_count))
                selected = random.sample(pool, k=actual_winners)
                winner_mentions = ", ".join(f"<@!{uid}>" for uid in selected)

                desc = f"Ended <t:{int(current_time)}:R>\nHosted by <@{int(host_id)}>\nWinner(s): {winner_mentions}"
                embed = discord.Embed(
                    description=desc,
                    color=0x2F3136,
                )
                embed.timestamp = discord.utils.utcnow()
                embed.set_author(name=prize)
                embed.set_footer(text="Ended at")

                with contextlib.suppress(discord.HTTPException):
                    await msg.edit(
                        content=(
                            f"{Emoji.GIVEAWAY.value} **GIVEAWAY ENDED** {Emoji.GIVEAWAY.value}\n>"
                            f"{Emoji.TADA.value} Congratulations! {winner_mentions} You won **{prize}**!"
                        ),
                        embed=embed,
                        view=None,
                    )

                # Store the user pool snapshot into DB before closing out, so we can run rerolls later
                await self.connection.execute(
                    "UPDATE Giveaway SET participants_json = ?, ends_at = 0 WHERE message_id = ?",
                    (participants_json, int(message_id)),
                )
                self._invalidate(int(message_id))
                self.join_view.participants.pop(int(message_id), None)

        await self.connection.commit()

    @commands.hybrid_command(name="gend", description="Ends an active giveaway early.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=True)
    async def gend(self, ctx: commands.Context, message_id: str) -> None:
        if "discord.com/channels/" in message_id or "discordapp.com/channels/" in message_id:
            message_id = message_id.split("/")[-1]
        try:
            msg_id = int(message_id)
        except ValueError:
            await ctx.send(f"{Emoji.WARNING.value} Invalid message ID format.", ephemeral=True)
            return

        gw = await self._fetch_giveaway(
            msg_id,
            "prize, host_id, winners, channel_id",
        )
        if not gw:
            await ctx.send(f"{Emoji.WARNING.value} Active giveaway matching this ID was not found.", ephemeral=True)
            return

        prize, host_id, winners_count, channel_id = gw

        # Perms Check: Original host OR User has Manage Channels permission in server
        is_host = ctx.author.id == int(host_id)
        is_admin = False
        if ctx.guild and isinstance(ctx.author, discord.Member):
            is_admin = ctx.author.guild_permissions.manage_channels

        if not (is_host or is_admin):
            await ctx.send(
                f"{Emoji.CROSS.value} Only the giveaway host or an administrator can end this giveaway.", ephemeral=True
            )
            return

        ch = discord.PartialMessageable(state=self.bot._connection, id=int(channel_id))
        msg = ch.get_partial_message(msg_id)

        pool = list(self.join_view.participants.get(msg_id, set()))
        if len(pool) < 1:
            desc = (
                f"{Emoji.WARNING.value} This giveaway ended with no participants. 😕\n"
                f"Hosted by <@{int(host_id)}>"
            )
            embed = discord.Embed(description=desc, color=0x2F3136)
            embed.set_author(name=prize)
            embed.timestamp = discord.utils.utcnow()
            embed.set_footer(text="Ended at")
            with contextlib.suppress(discord.HTTPException):
                await msg.edit(
                    content=f"{Emoji.GIVEAWAY.value} **GIVEAWAY ENDED** {Emoji.GIVEAWAY.value}",
                    embed=embed,
                    view=None,
                )
            await self.connection.execute("DELETE FROM Giveaway WHERE message_id = ?", (msg_id,))
            await self.connection.commit()
            self._invalidate(msg_id)
            await ctx.send(f"{Emoji.LIKE.value} Ended giveaway.", ephemeral=True)
            return

        actual_winners = min(len(pool), int(winners_count))
        selected = random.sample(pool, k=actual_winners)
        winner_mentions = ", ".join(f"<@!{uid}>" for uid in selected)

        embed = discord.Embed(
            description=f"Ended Early\nHosted by <@{int(host_id)}>\nWinner(s): {winner_mentions}", color=0x2F3136
        )
        embed.timestamp = discord.utils.utcnow()
        embed.set_author(name=prize)

        gw_ended = (
            f"{Emoji.GIVEAWAY.value} **GIVEAWAY ENDED** {Emoji.GIVEAWAY.value}\n>"
            f"{Emoji.TADA.value} Congratulations! {winner_mentions} You won **{prize}**!"
        )
        with contextlib.suppress(discord.HTTPException):
            await msg.edit(content=gw_ended, embed=embed, view=None)

        participants_json = json.dumps(pool)
        await self.connection.execute(
            "UPDATE Giveaway SET participants_json = ?, ends_at = 0 WHERE message_id = ?", (participants_json, msg_id)
        )
        self.join_view.participants.pop(msg_id, None)
        await self.connection.commit()
        self._invalidate(msg_id)
        await ctx.send(f"{Emoji.LIKE.value} Ended giveaway.", ephemeral=True)

    @commands.hybrid_command(name="greroll", description="Rerolls a finished giveaway to select new winners.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=True)
    async def greroll(self, ctx: commands.Context, message_id: str) -> None:
        if "discord.com/channels/" in message_id or "discordapp.com/channels/" in message_id:
            message_id = message_id.split("/")[-1]
        try:
            msg_id = int(message_id)
        except ValueError:
            await ctx.send(f"{Emoji.WARNING.value} Invalid message ID format.", ephemeral=True)
            return

        gw = await self._fetch_giveaway(
            msg_id,
            "prize, host_id, winners, channel_id, participants_json, ends_at",
        )
        if not gw:
            await ctx.send(f"{Emoji.WARNING.value} Giveaway record matching this ID was not found.", ephemeral=True)
            return

        prize, host_id, winners_count, channel_id, participants_json, ends_at = gw

        if float(ends_at) > 0:
            await ctx.send(
                f"{Emoji.WARNING.value} This giveaway is still running! "
                "Use `/gend` if you want to end it early instead.",
                ephemeral=True,
            )
            return

        # Perms Check: Original host OR User has Manage Channels permission in server
        is_host = ctx.author.id == int(host_id)
        is_admin = False
        if ctx.guild and isinstance(ctx.author, discord.Member):
            is_admin = ctx.author.guild_permissions.manage_channels

        if not (is_host or is_admin):
            await ctx.send(
                f"{Emoji.CROSS.value} Only the giveaway host or an administrator can reroll this giveaway.",
                ephemeral=True,
            )
            return

        try:
            pool = json.loads(participants_json)
        except Exception:
            pool = []

        if not pool or len(pool) < 1:
            await ctx.send(f"{Emoji.WARNING.value} There are no eligible participants to reroll from.", ephemeral=True)
            return

        actual_winners = min(len(pool), int(winners_count))
        selected = random.sample(pool, k=actual_winners)
        winner_mentions = ", ".join(f"<@!{uid}>" for uid in selected)

        # Edit the original giveaway message to show reroll info
        ch = discord.PartialMessageable(state=self.bot._connection, id=int(channel_id))
        msg = ch.get_partial_message(msg_id)

        reroll_desc = (
            f"{Emoji.SHINE.value} **Reroll Results**\n"
            f"{winner_mentions} {Emoji.TADA.value} Congratulations! "
            f"You have won the reroll for **{prize}**!"
        )
        with contextlib.suppress(discord.HTTPException):
            await msg.edit(content=reroll_desc)

        await ctx.send(
            f"{Emoji.TADA.value} {winner_mentions} You won the reroll for **{prize}**!\n"
            f"{Emoji.LIKE.value} Successfully rerolled the giveaway.",
            ephemeral=False,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(UserAppGiveaway(bot))
