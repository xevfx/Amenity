from __future__ import annotations

import io
import os
import re
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from api.log import FeedbackWebhook, log_exception

if TYPE_CHECKING:
    from core.amenity import Amenity

MAX_FEEDBACK_MESSAGE = 1000
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # Discord webhook upload limit


_IMAGE_TYPE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def _sanitize_filename(filename: str | None, content_type: str | None) -> str:
    extension = _IMAGE_TYPE_EXTENSIONS.get(content_type or "", ".png")
    stem = (filename or "image").rsplit(".", 1)[0]
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", stem)[:24].strip("_") or "image"
    return f"{stem}{extension}"


def _feedback_hook(kind: str) -> str | None:
    return os.getenv(f"{kind.upper()}_HOOK")


class Feedback(commands.Cog):
    display_name = "Feedback"
    group_name = "Feedback"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _reject(self, ctx: commands.Context, reason: str) -> None:
        embed = discord.Embed(
            title="Feedback Not Sent",
            description=reason,
            color=discord.Color.red(),
        )
        await ctx.send(embed=embed, ephemeral=ctx.interaction is not None)

    async def _submit(
        self,
        ctx: commands.Context,
        kind: str,
        message: str,
        image: discord.Attachment | None,
    ) -> None:
        if not message or not message.strip():
            await self._reject(ctx, "You need to include a message, for example `,suggestion Add a /weather command`.")
            return

        message = message.strip()
        if len(message) > MAX_FEEDBACK_MESSAGE:
            await self._reject(
                ctx,
                f"Please keep the message to {MAX_FEEDBACK_MESSAGE} characters or less.",
            )
            return

        file: discord.File | None = None
        if image is not None:
            if not (image.content_type or "").startswith("image/"):
                await self._reject(
                    ctx,
                    "The attached file must be an image so it can be sent with your message.",
                )
                return
            if image.size > MAX_IMAGE_BYTES:
                await self._reject(
                    ctx,
                    "The attached image is too large. Please keep it under 8 MB.",
                )
                return

        hook = _feedback_hook(kind)
        if not hook:
            await self._reject(
                ctx,
                f"{kind}s are not forwarded anywhere yet. The bot owner has to set up the "
                f"`{kind.upper()}_HOOK` webhook first ({kind.upper()}_HOOK needs a valid webhook URL in .env).",
            )
            return

        # A public defer must not be followed by ephemeral messages (the Discord
        # API rejects that combination), so keep every response on one path.
        await ctx.defer(ephemeral=ctx.interaction is not None)

        try:
            embed = discord.Embed(
                color=discord.Color.onyx_embed(),
                timestamp=datetime.now(),
                description=message,
            )
            embed.set_author(
                name=f"{kind} from {ctx.author} | {ctx.author.id}",
                icon_url=ctx.author.display_avatar.url if ctx.author.display_avatar else None,
            )
            embed.add_field(
                name="Server",
                value=f"{ctx.guild.name} [`{ctx.guild.id}`]" if ctx.guild else "DM",
                inline=False,
            )
            embed.add_field(
                name="Channel",
                value=f"{ctx.channel} [`{ctx.channel.id}`]" if ctx.guild else "DM",
                inline=False,
            )

            if image is not None:
                data = await image.read()
                filename = _sanitize_filename(image.filename, image.content_type)
                file = discord.File(io.BytesIO(data), filename=filename)
                embed.set_image(url=f"attachment://{filename}")

            sent = await FeedbackWebhook(embed, hook=hook, file=file)
        except Exception as exc:
            log_exception(exc)
            sent = False

        if not sent:
            await self._reject(ctx, "Your message could not be delivered right now. Please try again later.")
            return

        confirmation = discord.Embed(
            title=f"{kind} Sent",
            description="Thanks! Your message has been forwarded to the bot developers."
            + (" Your image is included." if image is not None else ""),
            color=discord.Color.onyx_embed(),
        )
        if len(message) > 200:
            confirmation.add_field(name="Your message", value=f"{message[:197]}...")
        await ctx.send(embed=confirmation, ephemeral=ctx.interaction is not None)

    @commands.hybrid_command(name="suggestion", description="Send a suggestion to the bot developers.")
    @app_commands.describe(
        message="What would you like to suggest?",
        image="Optional image or screenshot to attach.",
    )
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def suggestion(
        self,
        ctx: commands.Context,
        image: discord.Attachment | None,
        *,
        message: str,
    ) -> None:
        """Forward a suggestion to the bot developers through the feedback webhook."""
        await self._submit(ctx, "Suggestion", message, image)

    @commands.hybrid_command(name="report", description="Report a bug or issue with the bot.")
    @app_commands.describe(
        message="What went wrong? Describe the issue.",
        image="Optional image or screenshot to attach.",
    )
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def report(
        self,
        ctx: commands.Context,
        image: discord.Attachment | None,
        *,
        message: str,
    ) -> None:
        """Forward a bug report to the bot developers through the feedback webhook."""
        await self._submit(ctx, "Report", message, image)


async def setup(bot: Amenity) -> None:
    await bot.add_cog(Feedback(bot))
