from collections.abc import Sequence
from contextlib import suppress

import discord
from discord.ext import commands
from discord.ui import Button, View

from .emojis import Emoji


class Links:
    """Simple utility for creating Discord link buttons."""

    @staticmethod
    def button(label: str, url: str, emoji: str | None = None) -> Button:
        """Create a link button."""
        return Button(label=label, url=url, emoji=emoji, style=discord.ButtonStyle.link)

    @staticmethod
    def view(label: str, url: str, emoji: str | None = None) -> View:
        """Create a persistent view with one link button."""
        view = View(timeout=None)  # Link views don't need timeout
        view.add_item(Links.button(label, url, emoji))
        return view

    @staticmethod
    def multi_view(
        links: Sequence[tuple[str, str] | tuple[str, str, str]],
    ) -> View:
        """Create a persistent view with multiple link buttons.

        Args:
            links: List of tuples like [("Label", "URL"), ("Label", "URL", "emoji")]
        """
        view = View(timeout=None)  # Link views don't need timeout
        for link in links:
            if len(link) == 2:
                label, url = link
                emoji = None
            else:
                label, url, emoji = link
            view.add_item(Links.button(label, url, emoji))
        return view


class BotLinks:
    """Quick preset links for common bot needs."""

    def __init__(self, support_url: str | None = None, invite_url: str | None = None) -> None:
        self.support_url = support_url or "https://discord.gg/x4kaVDcubT"
        self.invite_url = invite_url or "https://discord.com/oauth2/authorize?client_id=1455170105666306113"

    def support(self, label: str = "Support Server", emoji: str = "🎗️") -> View:
        """Get support server link view."""
        return Links.view(label, self.support_url, emoji)

    def invite(self, label: str = "Invite Bot", emoji: str = "🤖") -> View:
        """Get bot invite link view."""
        return Links.view(label, self.invite_url, emoji)

    def both(self) -> View:
        """Get view with both support and invite buttons."""
        return Links.multi_view(
            [
                ("Support Server", self.support_url, "🎗️"),
                ("Invite Bot", self.invite_url, "🤖"),
            ]
        )


class ConfirmView(View):
    def __init__(
        self,
        author_id: int,
        *,
        confirm_label: str = f"{Emoji.DELETE.value} Confirm",
        cancel_label: str = f"{Emoji.CROSS.value} Cancel",
        confirm_style: discord.ButtonStyle = discord.ButtonStyle.danger,
        cancel_style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value: bool | None = None
        self.message: discord.Message | None = None

        confirm = Button(label=confirm_label, style=confirm_style)
        confirm.callback = self._confirm
        cancel = Button(label=cancel_label, style=cancel_style)
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This confirmation is not for you.",
                ephemeral=True,
            )
            return False
        return True

    async def _confirm(self, interaction: discord.Interaction) -> None:
        self.value = True
        await self._finish(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.value = False
        await self._finish(interaction)

    async def _finish(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            item.disabled = True
        with suppress(discord.HTTPException):
            if interaction.response.is_done():
                if interaction.message:
                    await interaction.message.edit(view=self)
            else:
                await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            with suppress(discord.HTTPException):
                await self.message.edit(view=self)


async def confirm_action(
    ctx: commands.Context,
    prompt: str,
    *,
    timeout: float = 30.0,
    ephemeral: bool = True,
    confirm_label: str = f"{Emoji.DELETE.value} Confirm",
    cancel_label: str = f"{Emoji.CROSS.value} Cancel",
    confirm_style: discord.ButtonStyle = discord.ButtonStyle.danger,
    cancel_style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    confirm_message: str | None = None,
    cancel_message: str | None = None,
    timeout_message: str | None = None,
) -> bool:
    view = ConfirmView(
        ctx.author.id,
        confirm_label=confirm_label,
        cancel_label=cancel_label,
        confirm_style=confirm_style,
        cancel_style=cancel_style,
        timeout=timeout,
    )

    if getattr(ctx, "interaction", None):
        if ctx.interaction.response.is_done():
            message = await ctx.interaction.followup.send(
                prompt,
                view=view,
                ephemeral=ephemeral,
            )
        else:
            await ctx.interaction.response.send_message(
                prompt,
                view=view,
                ephemeral=ephemeral,
            )
            message = await ctx.interaction.original_response()
    else:
        message = await ctx.send(prompt, view=view)

    view.message = message
    await view.wait()

    if view.value is True and confirm_message is not None:
        with suppress(discord.HTTPException):
            await message.edit(content=confirm_message, view=None)
    elif view.value is False and cancel_message is not None:
        with suppress(discord.HTTPException):
            await message.edit(content=cancel_message, view=None)
    elif view.value is None and timeout_message is not None:
        with suppress(discord.HTTPException):
            await message.edit(content=timeout_message, view=None)

    return view.value is True


# bot_links = BotLinks(
#     support_url="https://discord.gg/your-server",
#     invite_url="https://discord.com/oauth2/authorize?client_id=123456789&scope=bot"
# )
# SupportServerLink = bot_links.support()


# Usage:
"""
# Setup once
bot_links = BotLinks(
    support_url="https://discord.gg/your-server",
    invite_url="https://discord.com/oauth2/authorize?client_id=123456789&scope=bot"
)

# Use in commands
@bot.command()
async def support(ctx):
    await ctx.send("Join our support server!", view=bot_links.support())

@bot.command()
async def invite(ctx):
    await ctx.send("Invite me!", view=bot_links.invite())

@bot.command()
async def links(ctx):
    await ctx.send("Useful links:", view=bot_links.both())

# Custom links
custom_view = Links.multi_view([
    ("Website", "https://mysite.com", "🌐"),
    ("GitHub", "https://github.com/myrepo", "🐙")
])
"""
