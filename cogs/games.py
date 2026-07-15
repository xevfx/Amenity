import random

import discord
from discord import app_commands
from discord.ext import commands

from api.log import log_exception
from core.amenity import Amenity

SUITS = ("♠", "♥", "♦", "♣")
RANKS = ("2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A")


def _card_value(card: tuple[str, str]) -> int:
    rank = card[1]
    if rank == "A":
        return 11
    if rank in ("J", "Q", "K"):
        return 10
    return int(rank)


def _hand_value(hand: list[tuple[str, str]]) -> int:
    total = sum(_card_value(c) for c in hand)
    aces = sum(1 for c in hand if c[1] == "A")
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def _hand_display(hand: list[tuple[str, str]]) -> str:
    return " ".join(f"`{s}{r}`" for s, r in hand)


def _deck() -> list[tuple[str, str]]:
    d = [(s, r) for s in SUITS for r in RANKS]
    random.shuffle(d)
    return d


class TicTacToeButton(discord.ui.Button["TicTacToeView"]):
    def __init__(self, x: int, y: int) -> None:
        # custom_id helps keep track of the board coordinates (row x, column y)
        super().__init__(style=discord.ButtonStyle.secondary, label="\u200b", row=x)
        self.x = x
        self.y = y

    async def callback(self, interaction: discord.Interaction) -> None:
        view: TicTacToeView = self.view

        # Enforce that only the current player can make a move
        if interaction.user.id != view.current_player_id:
            await interaction.response.send_message("It's not your turn!", ephemeral=True)
            return

        # Determine the symbol and update button state
        if view.turn == view.X:
            self.label = "X"
            self.style = discord.ButtonStyle.danger
            self.disabled = True
            view.board[self.x][self.y] = view.X
            view.turn = view.O
            view.current_player_id = view.player_o.id
        else:
            self.label = "O"
            self.style = discord.ButtonStyle.success
            self.disabled = True
            view.board[self.x][self.y] = view.O
            view.turn = view.X
            view.current_player_id = view.player_x.id

        # Check game states
        winner = view.check_winner()
        if winner is not None:
            if winner == view.X:
                content = f"🎉 **{view.player_x.mention} (X) wins!**"
            elif winner == view.O:
                content = f"🎉 **{view.player_o.mention} (O) wins!**"
            else:
                content = "🤝 **It's a tie!**"

            # Disable all buttons when game ends
            for child in view.children:
                child.disabled = True

            view.stop()
        else:
            # Game continues, update turn prompt
            current_mention = view.player_x.mention if view.turn == view.X else view.player_o.mention
            content = (
                f"🎮 Tic-Tac-Toe: {view.player_x.mention} (X) vs "
                f"{view.player_o.mention} (O)\n➡️ Current Turn: {current_mention}"
            )

        await interaction.response.edit_message(content=content, view=view)


class TicTacToeView(discord.ui.View):
    X = 1
    O = -1  # noqa: E741
    TIE = 2

    def __init__(self, player_x: discord.User, player_o: discord.User) -> None:
        super().__init__(timeout=180.0)  # 3-minute timeout
        self.player_x = player_x
        self.player_o = player_o
        self.turn = self.X
        self.current_player_id = player_x.id

        # Initialize a 3x3 internal tracking grid
        self.board = [
            [0, 0, 0],
            [0, 0, 0],
            [0, 0, 0],
        ]

        # Dynamically generate and attach the 9 buttons
        for x in range(3):
            for y in range(3):
                self.add_item(TicTacToeButton(x, y))

    def check_winner(self) -> int | None:
        # Check rows
        for row in self.board:
            value = sum(row)
            if value == 3:
                return self.X
            if value == -3:
                return self.O

        # Check columns
        for col in range(3):
            value = self.board[0][col] + self.board[1][col] + self.board[2][col]
            if value == 3:
                return self.X
            if value == -3:
                return self.O

        # Check diagonals
        diag1 = self.board[0][0] + self.board[1][1] + self.board[2][2]
        diag2 = self.board[0][2] + self.board[1][1] + self.board[2][0]
        if diag1 == 3 or diag2 == 3:
            return self.X
        if diag1 == -3 or diag2 == -3:
            return self.O

        # Check for tie (no empty spaces left)
        if all(cell != 0 for row in self.board for cell in row):
            return self.TIE

        return None


class MinesTileButton(discord.ui.Button["MinesView"]):
    def __init__(self, x: int, y: int) -> None:
        # 5x5 grid means row index corresponds to x
        super().__init__(style=discord.ButtonStyle.secondary, label="\u200b", row=x)
        self.x = x
        self.y = y

    async def callback(self, interaction: discord.Interaction) -> None:
        view: MinesView = self.view

        # Guard: Only the game initiator can play
        if interaction.user.id != view.player_id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return

        # Check if hit a bomb or a diamond
        if view.grid[self.x][self.y] == "B":
            # Exploded! Game Over
            self.style = discord.ButtonStyle.danger
            self.label = "💥"
            await view.end_game(interaction, won=False)
        else:
            # Found a diamond
            self.style = discord.ButtonStyle.success
            self.label = "💎"
            self.disabled = True
            view.diamonds_found += 1

            # Check if all safe tiles are cleared (Absolute Win condition)
            if view.diamonds_found == view.total_diamonds:
                await view.end_game(interaction, won=True)
            else:
                # Update grid view and continue playing
                await interaction.response.edit_message(content=view.get_status_message(), view=view)


class MinesView(discord.ui.View):
    def __init__(self, player_id: int, num_mines: int) -> None:
        super().__init__(timeout=180.0)  # 3-minute timeout
        self.player_id = player_id
        self.num_mines = num_mines
        self.diamonds_found = 0
        self.total_diamonds = 25 - num_mines

        # Setup internal 5x5 grid layout
        # 'D' for Diamond, 'B' for Bomb
        tiles = ["B"] * num_mines + ["D"] * (25 - num_mines)
        random.shuffle(tiles)

        self.grid = [tiles[i : i + 5] for i in range(0, 25, 5)]

        # Dynamically append grid tiles
        for x in range(5):
            for y in range(5):
                self.add_item(MinesTileButton(x, y))

    def get_status_message(self) -> str:
        return (
            f"🧨 **Mines Game**\n"
            f"Total Mines: `{self.num_mines}` | "
            f"Safe Tiles Remaining: `{self.total_diamonds - self.diamonds_found}`\n"
            f"💎 **Diamonds Cleared:** `{self.diamonds_found}/{self.total_diamonds}`"
        )

    async def end_game(self, interaction: discord.Interaction, won: bool) -> None:
        self.stop()

        # Reveal the full board map to the player
        for child in self.children:
            child.disabled = True
            if isinstance(child, MinesTileButton):
                if self.grid[child.x][child.y] == "B":
                    child.label = "💣" if child.label != "💥" else "💥"
                    if child.label != "💥":
                        child.style = discord.ButtonStyle.secondary
                else:
                    child.label = "💎"

        # Determine structural header state string
        if won:
            header = "🏆 **Perfect Game!** You successfully cleared every single diamond without triggering a mine!"
        else:
            header = f"💥 **BOOM!** You hit a mine after discovering `{self.diamonds_found}` diamonds. Game Over!"

        await interaction.response.edit_message(content=f"{header}\n\n{self.get_status_message()}", view=self)


class Games(commands.Cog):
    """Simple games: ttt (multiplayer Tic-Tac-Toe), cf (coin flip), mines (single-player).

    Implementations are small and synchronous where possible to keep behavior
    predictable. Tic-tac-toe supports a simple two-player challenge using
    reactions/buttons.
    """

    display_name = "Games"
    group_name = "Games"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _send_embed(
        self,
        ctx: commands.Context,
        description: str,
        title: str | None = None,
        color: discord.Color = discord.Color.blurple(),
        ephemeral: bool = False,
    ) -> None:
        embed = discord.Embed(description=description, color=color)
        if title:
            embed.title = title
        if ctx.interaction:
            # prefer followup if initial response is done
            if ctx.interaction.response.is_done():
                await ctx.interaction.followup.send(embed=embed, ephemeral=ephemeral)
            else:
                await ctx.interaction.response.send_message(embed=embed, ephemeral=ephemeral)
            return
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="coinflip", description="Flip a coin.")
    @app_commands.describe(side="Optional: call heads or tails")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def coin_flip(self, ctx: commands.Context, side: str | None = None) -> None:
        try:
            side = side.lower() if side else None
            result = random.choice(["heads", "tails"])

            if side in ("heads", "tails", "h", "t"):
                # Determine if the user's guess matches the result
                win = (side.startswith("h") and result == "heads") or (side.startswith("t") and result == "tails")

                await self._send_embed(
                    ctx,
                    f"The coin landed **{result.upper()}**. {'You win!' if win else 'You lose.'}",
                    title="Coin Flip",
                    color=discord.Color.green() if win else discord.Color.red(),
                )
            else:
                await self._send_embed(ctx, f"The coin landed **{result.upper()}**.", title="Coin Flip")
        except Exception as exc:
            log_exception(exc)
            await self._send_embed(ctx, "An error occurred while flipping the coin.", ephemeral=True)

    @commands.hybrid_command(name="blackjack", description="Play Blackjack with another user.", aliases=["bj"])
    @app_commands.describe(opponent="User to challenge")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def blackjack(self, ctx: commands.Context, opponent: discord.User | None = None) -> None:
        await self._blackjack(ctx, opponent)

    @commands.hybrid_command(name="bj", description="Play Blackjack with another user.")
    @app_commands.describe(opponent="User to challenge")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def blackjack_alias(self, ctx: commands.Context, opponent: discord.User | None = None) -> None:
        await self._blackjack(ctx, opponent)

    async def _blackjack(self, ctx: commands.Context, opponent: discord.User | None = None) -> None:
        try:
            if opponent is None:
                command_name = ctx.command.qualified_name if ctx.command else "blackjack"
                await self._send_embed(ctx, f"Usage: /{command_name} @opponent", ephemeral=True)
                return
            if opponent.bot:
                await self._send_embed(ctx, "You cannot challenge a bot.", ephemeral=True)
                return
            if opponent.id == ctx.author.id:
                await self._send_embed(ctx, "You cannot challenge yourself.", ephemeral=True)
                return

            class BJChallengeView(discord.ui.View):
                def __init__(self) -> None:
                    super().__init__(timeout=30)
                    self.accepted: bool | None = None

                @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
                async def accept_bj(inner_self, interaction: discord.Interaction, btn: discord.ui.Button) -> None:
                    if interaction.user.id != opponent.id:
                        await interaction.response.send_message("Only the challenged user may accept.", ephemeral=True)
                        return
                    inner_self.accepted = True
                    for child in inner_self.children:
                        child.disabled = True
                    await interaction.response.edit_message(
                        content=f"{opponent.mention} accepted the challenge!", view=inner_self
                    )
                    inner_self.stop()

                @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
                async def decline_bj(inner_self, interaction: discord.Interaction, btn: discord.ui.Button) -> None:
                    if interaction.user.id != opponent.id:
                        await interaction.response.send_message("Only the challenged user may decline.", ephemeral=True)
                        return
                    inner_self.accepted = False
                    for child in inner_self.children:
                        child.disabled = True
                    await interaction.response.edit_message(content="Challenge declined.", view=inner_self)
                    inner_self.stop()

            chal_view = BJChallengeView()
            chal_msg = await ctx.send(
                f"{opponent.mention}, you have been challenged to Blackjack by {ctx.author.mention}.",
                view=chal_view,
            )

            timed_out = await chal_view.wait()
            if timed_out or chal_view.accepted is None:
                await chal_msg.edit(content="Challenge timed out.", view=None)
                return
            if not chal_view.accepted:
                return

            # Setup Game State
            players = [ctx.author, opponent]
            deck = _deck()
            hands: dict[int, list[tuple[str, str]]] = {
                ctx.author.id: [],
                opponent.id: [],
            }

            interacted_players: set[int] = set()

            for _ in range(2):
                hands[ctx.author.id].append(deck.pop())
                hands[opponent.id].append(deck.pop())

            class BJView(discord.ui.View):
                def __init__(self, timeout: float | None = 300) -> None:
                    super().__init__(timeout=timeout)
                    self.finished = False
                    self.finished_players: set[int] = set()
                    self.hand_messages: dict[int, discord.WebhookMessage] = {}

                def build_main_embed(self) -> discord.Embed:
                    status = "**Game Over**" if self.finished else "Click **View My Hand** to see your cards privately!"
                    embed = discord.Embed(title="♠ Blackjack ♥", description=status, color=discord.Color.green())

                    for p in players:
                        val = _hand_value(hands[p.id])
                        if self.finished:
                            line = f"**Hand:** {_hand_display(hands[p.id])} = **{val}**"
                            if val > 21:
                                line += " 💥 (Bust)"
                        else:
                            if p.id not in interacted_players:
                                line = f"**Hand:** {_hand_display(hands[p.id])} = **{val}**"
                                if val == 21:
                                    line += " ✨ (Blackjack!)"
                                elif val > 21:
                                    line += " 💥 (Bust)"
                            else:
                                initial_two_cards = hands[p.id][:2]
                                initial_value = _hand_value(initial_two_cards)
                                line = f"**Hand:** {_hand_display(initial_two_cards)} = **{initial_value}**"

                        name = getattr(p, "global_name", None) or p.name
                        embed.add_field(name=name, value=line, inline=False)
                    return embed

                def build_hand_embed(self, pid: int) -> discord.Embed:
                    val = _hand_value(hands[pid])
                    status_text = "💥 BUSTED!" if val > 21 else ("✨ BLACKJACK!" if val == 21 else "Active Hand")
                    return discord.Embed(
                        title="Your Private Hand",
                        description=(f"{_hand_display(hands[pid])}\n**Your Total Value:** {val} ({status_text})"),
                        color=discord.Color.blue(),
                    )

                def check_game_over(self) -> bool:
                    # FIX: Auto-lock players who hit 21 or bust so checking is completely accurate
                    for p in players:
                        if _hand_value(hands[p.id]) >= 21:
                            self.finished_players.add(p.id)

                    for p in players:
                        if p.id not in self.finished_players:
                            return False
                    self.finished = True
                    return True

                def final_result(self) -> str:
                    p1, p2 = players
                    v1, v2 = _hand_value(hands[p1.id]), _hand_value(hands[p2.id])
                    if v1 > 21 and v2 > 21:
                        return "Both bust! It's a tie."
                    if v1 > 21 or (v2 <= 21 and v2 > v1):
                        return f"{p2.mention} wins!"
                    if v2 > 21 or (v1 <= 21 and v1 > v2):
                        return f"{p1.mention} wins!"
                    return "It's a tie!"

            class ActionButton(discord.ui.Button):
                def __init__(self, action: str) -> None:
                    label = {"hit": "Hit", "stand": "Stand", "view_hand": "View My Hand"}[action]
                    if action == "hit":
                        style = discord.ButtonStyle.primary
                        row = 0
                    elif action == "stand":
                        style = discord.ButtonStyle.secondary
                        row = 0
                    else:
                        style = discord.ButtonStyle.success
                        row = 1
                    super().__init__(style=style, label=label, row=row)
                    self.action = action

                async def callback(inner_self, interaction: discord.Interaction) -> None:
                    bj_view: BJView = inner_self.view  # type: ignore[assignment]
                    if interaction.user.id not in [p.id for p in players]:
                        await interaction.response.send_message("You are not a player in this game.", ephemeral=True)
                        return

                    pid = interaction.user.id

                    if inner_self.action == "view_hand":
                        await interaction.response.send_message(embed=bj_view.build_hand_embed(pid), ephemeral=True)
                        bj_view.hand_messages[pid] = await interaction.original_response()
                        return

                    if bj_view.finished:
                        await interaction.response.send_message("Game already finished.", ephemeral=True)
                        return

                    if pid in bj_view.finished_players or _hand_value(hands[pid]) >= 21:
                        await interaction.response.send_message(
                            "Your choices are locked in. Waiting on your opponent.", ephemeral=True
                        )
                        return

                    if inner_self.action == "hit":
                        hands[pid].append(deck.pop())
                        interacted_players.add(pid)
                        # FIX: Directly finalize the player if they hit 21 or bust
                        if _hand_value(hands[pid]) >= 21:
                            bj_view.finished_players.add(pid)
                    elif inner_self.action == "stand":
                        interacted_players.add(pid)
                        bj_view.finished_players.add(pid)

                    if bj_view.check_game_over():
                        for child in bj_view.children:
                            child.disabled = True
                        embed = bj_view.build_main_embed()
                        embed.description = f"**Game Over**\n{bj_view.final_result()}"
                        await interaction.response.edit_message(embed=embed, view=bj_view)
                    else:
                        embed = bj_view.build_main_embed()
                        await interaction.response.edit_message(embed=embed, view=bj_view)
                        if pid in bj_view.hand_messages:
                            try:
                                await bj_view.hand_messages[pid].edit(embed=bj_view.build_hand_embed(pid))
                            except discord.NotFound:
                                bj_view.hand_messages.pop(pid, None)

            view = BJView()
            view.add_item(ActionButton("hit"))
            view.add_item(ActionButton("stand"))
            view.add_item(ActionButton("view_hand"))

            # FIX: Check natural deal conditions before starting layout processing
            p1_val = _hand_value(hands[ctx.author.id])
            p2_val = _hand_value(hands[opponent.id])

            # If anyone naturally deals a 21 or busts at startup, push them to finished state right away
            if p1_val >= 21:
                view.finished_players.add(ctx.author.id)
            if p2_val >= 21:
                view.finished_players.add(opponent.id)

            if view.check_game_over():
                for child in view.children:
                    child.disabled = True
                embed = view.build_main_embed()
                embed.description = f"**Game Over**\n{view.final_result()}"
                await chal_msg.edit(content=None, embed=embed, view=view)
            else:
                await chal_msg.edit(content=None, embed=view.build_main_embed(), view=view)

        except Exception as exc:
            log_exception(exc)
            if ctx.interaction:
                await ctx.send("An error occurred while playing Blackjack.", ephemeral=True)
            else:
                await ctx.send("An error occurred while playing Blackjack.")

    @commands.hybrid_command(name="tic-tac-toe", description="Play a game of Tic-Tac-Toe using buttons.", aliases=["ttt"])
    @app_commands.describe(opponent="Optional: The user you want to challenge")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def tictactoe(self, ctx: commands.Context, opponent: discord.User | None = None) -> None:
        await self._tictactoe(ctx, opponent)

    @commands.hybrid_command(name="ttt", description="Play a game of Tic-Tac-Toe using buttons.")
    @app_commands.describe(opponent="Optional: The user you want to challenge")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def tictactoe_alias(self, ctx: commands.Context, opponent: discord.User | None = None) -> None:
        await self._tictactoe(ctx, opponent)

    async def _tictactoe(self, ctx: commands.Context, opponent: discord.User | None = None) -> None:
        # Prevent playing against oneself
        if opponent == ctx.author:
            await self._send_embed(
                ctx,
                "You cannot play against yourself!",
                title="Error",
                color=discord.Color.red(),
                ephemeral=True,
            )
            return

        # Default to the bot if no opponent is provided
        target_opponent = opponent or ctx.bot.user

        # Initialize the game view
        view = TicTacToeView(player_x=ctx.author, player_o=target_opponent)

        content = (
            f"🎮 Tic-Tac-Toe: {ctx.author.mention} (X) vs "
            f"{target_opponent.mention} (O)\n➡️ Current Turn: {ctx.author.mention}"
        )

        # Handle interaction/prefix differences gracefully matching your structural pattern
        if ctx.interaction:
            if ctx.interaction.response.is_done():
                await ctx.interaction.followup.send(content=content, view=view, ephemeral=False)
            else:
                await ctx.interaction.response.send_message(content=content, view=view, ephemeral=False)
            return

        await ctx.send(content=content, view=view)

    @commands.hybrid_command(name="mines", description="Play a game of classic Mines to the finish.")
    @app_commands.describe(mines="The number of hidden mines on the board (Between 1 and 24)")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def mines(self, ctx: commands.Context, mines: int) -> None:
        # Validate grid configuration bounds
        if mines < 1 or mines > 24:
            await self._send_embed(
                ctx,
                description=("Please choose a realistic challenge level! Mines must be between **1** and **24**."),
                title="Invalid Configuration",
                color=discord.Color.red(),
                ephemeral=True,
            )
            return

        view = MinesView(player_id=ctx.author.id, num_mines=mines)
        content = view.get_status_message()

        # Follow framework context processing rules
        if ctx.interaction:
            if ctx.interaction.response.is_done():
                await ctx.interaction.followup.send(content=content, view=view, ephemeral=False)
            else:
                await ctx.interaction.response.send_message(content=content, view=view, ephemeral=False)
            return

        await ctx.send(content=content, view=view)


async def setup(bot: Amenity) -> None:
    await bot.add_cog(Games(bot))
