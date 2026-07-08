import os
import re
from datetime import datetime
from pathlib import Path

import discord
import qrcode
from discord import app_commands
from discord.ext import commands
from googletrans import LANGUAGES, Translator
from PIL import Image, ImageColor, ImageDraw, ImageFilter
from simpleeval import simple_eval

from api.crypto_networks import CRYPTO_NETWORK_BY_VALUE, CRYPTO_NETWORK_CHOICES, crypto_qr_payload
from api.emojis import Emoji
from api.http import close_http_session, create_http_session
from api.log import log_exception
from core.amenity import Amenity

QR_GRADIENT = ("#0B0B0D", "#1A1F2E")
QR_BACK_COLOR = "white"
QR_GLOW_COLOR = "#1e88e5"
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FIELD_VALUE_LIMIT = 1024
CODE_BLOCK_OVERHEAD = 8


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    return f"{value[: limit - 1]}…"


def _code_block(value: str, *, limit: int = EMBED_FIELD_VALUE_LIMIT) -> str:
    truncated = _truncate(value.replace("```", "`\u200b``"), limit - CODE_BLOCK_OVERHEAD)
    return f"```\n{truncated}\n```"


def _safe_unlink(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        return


def _build_qr(data: str, *, box_size: int = 10, border: int = 4) -> qrcode.QRCode:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)
    return qr


def _apply_gradient(
    base_img: Image.Image,
    *,
    back_color: str,
    gradient_colors: tuple[str, str],
    direction: str,
) -> Image.Image:
    width, height = base_img.size
    color1 = ImageColor.getrgb(gradient_colors[0])
    color2 = ImageColor.getrgb(gradient_colors[1])

    if direction == "horizontal":
        gradient = Image.new("RGB", (width, 1))
        draw = ImageDraw.Draw(gradient)
        for x in range(width):
            ratio = x / max(width - 1, 1)
            r = int(color1[0] + (color2[0] - color1[0]) * ratio)
            g = int(color1[1] + (color2[1] - color1[1]) * ratio)
            b = int(color1[2] + (color2[2] - color1[2]) * ratio)
            draw.line((x, 0, x, 1), fill=(r, g, b))
        gradient = gradient.resize((width, height))
    else:
        gradient = Image.new("RGB", (1, height))
        draw = ImageDraw.Draw(gradient)
        for y in range(height):
            ratio = y / max(height - 1, 1)
            r = int(color1[0] + (color2[0] - color1[0]) * ratio)
            g = int(color1[1] + (color2[1] - color1[1]) * ratio)
            b = int(color1[2] + (color2[2] - color1[2]) * ratio)
            draw.line((0, y, 1, y), fill=(r, g, b))
        gradient = gradient.resize((width, height))

    mask = base_img.convert("L")
    final_img = Image.new("RGB", (width, height), ImageColor.getrgb(back_color))
    final_img.paste(gradient, (0, 0), mask)
    return final_img


def _apply_glow(
    qr: qrcode.QRCode,
    base_img: Image.Image,
    *,
    box_size: int,
    border: int,
    glow_color: str,
    glow_size: int,
) -> Image.Image:
    width, height = base_img.size
    glow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow_layer)
    glow_rgb = ImageColor.getrgb(glow_color)

    for y in range(qr.modules_count):
        for x in range(qr.modules_count):
            if qr.modules[y][x]:
                bbox = (
                    x * box_size + border - glow_size,
                    y * box_size + border - glow_size,
                    (x + 1) * box_size + border + glow_size,
                    (y + 1) * box_size + border + glow_size,
                )
                draw.ellipse(bbox, fill=glow_rgb + (128,))

    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(max(glow_size // 2, 1)))
    combined = Image.alpha_composite(glow_layer, base_img.convert("RGBA"))
    return combined


def _resolve_language(value: str) -> tuple[str, str] | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    normalized = normalized.replace("_", "-")
    if normalized in LANGUAGES:
        return normalized, LANGUAGES[normalized]
    reverse = {name.lower(): code for code, name in LANGUAGES.items()}
    code = reverse.get(normalized)
    if code:
        return code, LANGUAGES[code]
    return None


def generate_qr(
    data: str,
    filename: str,
    *,
    box_size: int = 10,
    border: int = 4,
    fill: str = "black",
    back_color: str = QR_BACK_COLOR,
    fill_gradient: tuple[str, str] | None = None,
    gradient_direction: str = "vertical",
    add_glow: bool = False,
    glow_color: str = QR_GLOW_COLOR,
    glow_size: int = 10,
) -> bool:
    if not data or not isinstance(data, str):
        return False

    try:
        file_path = Path(filename)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        qr = _build_qr(data, box_size=box_size, border=border)
        if not fill_gradient and not add_glow:
            img = qr.make_image(fill_color=fill, back_color=back_color)
            img.save(filename)
            return True

        base_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        if fill_gradient:
            final_img = _apply_gradient(
                base_img,
                back_color=back_color,
                gradient_colors=fill_gradient,
                direction=gradient_direction,
            )
        else:
            final_img = base_img

        if add_glow:
            final_img = _apply_glow(
                qr,
                final_img,
                box_size=box_size,
                border=border,
                glow_color=glow_color,
                glow_size=glow_size,
            )

        final_img.save(filename)
        return True
    except Exception as exc:
        log_exception(exc)
        return False


class UserUtility(commands.Cog):
    display_name = "User Utility"
    group_name = "Utilities"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.aiohttp = create_http_session()
        self.translator = Translator()
        self.translate_to_english_menu = app_commands.ContextMenu(
            name="translate-to-english",
            callback=self.translate_to_english,
            type=discord.AppCommandType.message,
        )
        self.bot.tree.add_command(self.translate_to_english_menu)

    def cog_unload(self) -> None:
        close_http_session(self.aiohttp, self.bot.loop)
        self.bot.tree.remove_command(
            self.translate_to_english_menu.name,
            type=self.translate_to_english_menu.type,
        )

    def _format_user_badges(self, user: discord.User) -> str:
        badge_map = [
            ("staff", "Staff"),
            ("partner", "Partner"),
            ("hypesquad", "HypeSquad"),
            ("bug_hunter", "Bug Hunter"),
            ("hypesquad_bravery", "House Bravery"),
            ("hypesquad_brilliance", "House Brilliance"),
            ("hypesquad_balance", "House Balance"),
            ("early_supporter", "Early Supporter"),
            ("team_user", "Team User"),
            ("system", "System"),
            ("bug_hunter_level_2", "Bug Hunter Level 2"),
            ("verified_bot", "Verified Bot"),
            ("verified_bot_developer", "Verified Bot Developer"),
            ("certified_moderator", "Certified Moderator"),
            ("bot_http_interactions", "Bot HTTP Interactions"),
            ("active_developer", "Active Developer"),
            ("spammer", "Spammer"),
            ("quarantined", "Quarantined"),
        ]
        badges = [label for attr, label in badge_map if getattr(user.public_flags, attr, False)]
        if getattr(user, "premium_type", None):
            badges.append("Nitro")
        return ", ".join(badges) if badges else "None"

    @commands.hybrid_command(name="ping", description="Check the bot's latency.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def ping(self, ctx: commands.Context) -> None:
        latency = self.bot.latency * 1000
        await ctx.reply(f"Latency: {latency:.2f} ms", ephemeral=True, mention_author=False)

    @commands.hybrid_command(name="avatar", description="Fetch a user's avatar.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(user="The user to fetch the avatar for.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def avatar(self, ctx: commands.Context, user: discord.User | None = None) -> None:
        target = user or ctx.author
        avatar = target.display_avatar
        embed = discord.Embed(
            title=f"Avatar: {target}",
            color=discord.Color.blue(),
        )
        embed.set_image(url=avatar.url)
        embed.set_footer(
            text=f"Requested by {ctx.author}",
            icon_url=ctx.author.display_avatar.url,
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="userinfo", description="Get information about a user.")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(user="The user to inspect.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def userinfo(self, ctx: commands.Context, user: discord.User | None = None) -> None:
        target = user or ctx.author

        try:
            fetched_user = await self.bot.fetch_user(target.id)
        except discord.HTTPException:
            fetched_user = None

        profile = fetched_user or target
        created_timestamp = int(profile.created_at.timestamp())
        banner = getattr(profile, "banner", None)
        accent_color = getattr(profile, "accent_color", None)

        embed = discord.Embed(
            title=f"User info: {profile}",
            color=accent_color or discord.Color.blurple(),
        )
        embed.set_author(name=profile.display_name, icon_url=profile.display_avatar.url)
        embed.set_thumbnail(url=profile.display_avatar.url)
        if banner is not None:
            embed.set_image(url=banner.url)

        embed.add_field(name="Username", value=f"`{profile.name}`", inline=True)
        embed.add_field(name="Display Name", value=f"`{profile.display_name}`", inline=True)
        embed.add_field(name="User ID", value=f"`{profile.id}`", inline=True)
        embed.add_field(name="Account Type", value="Bot" if profile.bot else "User", inline=True)
        embed.add_field(name="Created At", value=f"<t:{created_timestamp}:F>\n<t:{created_timestamp}:R>", inline=False)
        embed.add_field(name="Badges", value=self._format_user_badges(profile), inline=False)
        embed.add_field(name="Avatar", value=f"[Open avatar]({profile.display_avatar.url})", inline=True)
        embed.add_field(
            name="Banner",
            value=f"[Open banner]({banner.url})" if banner is not None else "None",
            inline=True,
        )
        if accent_color is not None:
            embed.add_field(name="Accent Color", value=f"`{accent_color}`", inline=True)

        embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="banner", description="Fetch a user's banner.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(user="The user to fetch the banner for.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def banner(self, ctx: commands.Context, user: discord.User | None = None) -> None:
        target = user or ctx.author
        try:
            fetched = await self.bot.fetch_user(target.id)
        except discord.HTTPException:
            fetched = None

        banner = fetched.banner if fetched else None
        if banner is None:
            await ctx.reply(
                f"{target} does not have a banner.",
                mention_author=False,
            )
            return

        embed = discord.Embed(
            title=f"Banner: {target}",
            color=discord.Color.blurple(),
        )
        embed.set_image(url=banner.url)
        embed.set_footer(
            text=f"Requested by {ctx.author}",
            icon_url=ctx.author.display_avatar.url,
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_group(
        name="qr",
        invoke_without_command=True,
        with_app_command=True,
        aliases=["qrcode"],
        description="Generate various types of QR codes.",
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.max_concurrency(15, commands.BucketType.default, wait=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def qr_group(self, ctx: commands.Context) -> None:
        await ctx.reply("Use /qr", delete_after=5, ephemeral=True, mention_author=False)

    async def _send_qr(
        self,
        ctx: commands.Context,
        *,
        filename: str,
        title: str,
        description: str | None = None,
        color: discord.Color = 0x00FFFF,
        fields: list[tuple[str, str]] | None = None,
    ) -> None:
        safe_description = _truncate(description, EMBED_DESCRIPTION_LIMIT) if description is not None else None
        embed = discord.Embed(title=title, description=safe_description, color=color)
        if fields:
            for name, value in fields:
                embed.add_field(name=name, value=_truncate(value, EMBED_FIELD_VALUE_LIMIT), inline=False)
        embed.set_image(url=f"attachment://{filename}")
        embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed, file=discord.File(filename))
        _safe_unlink(filename)

    def _qr_path(self, ctx: commands.Context, prefix: str) -> str:
        return f"{prefix}qr_{ctx.author.id}.png"

    @qr_group.command(name="text", description="Create a QR with custom text", aliases=["txt"])
    @app_commands.describe(txt="The text to encode in the QR code (max 200 characters).")
    async def qr_text(self, ctx: commands.Context, *, txt: str) -> None:
        if len(txt) > 200:
            await ctx.send(
                embed=discord.Embed(
                    description="Text too long. Please limit to 200 characters.",
                    color=discord.Color.red(),
                ),
                delete_after=5,
                ephemeral=True,
            )
            return

        filename = self._qr_path(ctx, "text")
        if not generate_qr(txt, filename, fill_gradient=QR_GRADIENT):
            await ctx.send(
                embed=discord.Embed(
                    description="Failed to generate QR code.",
                    color=discord.Color.red(),
                )
            )
            return

        await self._send_qr(
            ctx,
            filename=filename,
            title="Text QR Code",
            description=f"**Content:** {txt}",
        )

    @qr_group.command(name="crypto", description="Create a QR for a crypto address")
    @app_commands.describe(network="The crypto network", addy="The address to encode in the QR code.")
    @app_commands.choices(network=CRYPTO_NETWORK_CHOICES)
    async def qr_crypto(self, ctx: commands.Context, network: str, *, addy: str) -> None:
        network = network.strip().lower()
        network_data = CRYPTO_NETWORK_BY_VALUE.get(network)
        if not network_data:
            await ctx.send(
                embed=discord.Embed(
                    description="Please choose a supported crypto network.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        addy_value = addy.strip()
        if not addy_value:
            await ctx.send(
                embed=discord.Embed(
                    description="Usage: /qr crypto <network> <addy>",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        filename = self._qr_path(ctx, network)
        qr_payload = crypto_qr_payload(network_data, addy_value)
        if not generate_qr(qr_payload, filename, fill_gradient=QR_GRADIENT):
            await ctx.send(
                embed=discord.Embed(
                    description="Failed to generate QR code.",
                    color=discord.Color.red(),
                )
            )
            return

        await self._send_qr(
            ctx,
            filename=filename,
            title=f"{network_data.name} QR Code",
            description=f"Addy: {addy_value}",
            fields=[("Payload", qr_payload)],
        )

    @qr_group.command(
        name="upi",
        description="Create a QR for UPI with optional amount",
        aliases=["inr"],
    )
    @app_commands.describe(
        upi_id="The UPI ID to encode in the QR code.",
        amount="The amount to include in the UPI QR code (optional).",
        note="A note to include in the UPI QR code (optional, max 100 characters).",
    )
    async def qr_upi(
        self,
        ctx: commands.Context,
        upi_id: str,
        amount: float | None = None,
        *,
        note: str | None = None,
    ) -> None:
        upi_value = upi_id.strip()
        if not upi_value:
            await ctx.send(
                embed=discord.Embed(
                    description="Usage: /qrcode upi <upi_id> [amount] [note]",
                    color=discord.Color.red(),
                )
            )
            return
        if len(upi_value) > 25:
            await ctx.send(
                embed=discord.Embed(
                    description="UPI ID too long. Please limit to 25 characters.",
                    color=discord.Color.red(),
                ),
                delete_after=5,
                ephemeral=True,
            )
            return
        if note and len(note) > 100:
            await ctx.send(
                embed=discord.Embed(
                    description="Note too long. Please limit to 100 characters.",
                    color=discord.Color.red(),
                ),
                delete_after=5,
                ephemeral=True,
            )
            return
        url = f"upi://pay?pa={upi_value}&pn=RecipientName"
        if amount:
            url += f"&am={amount}&cu=INR"

        filename = self._qr_path(ctx, "upi")
        if not generate_qr(url, filename, fill_gradient=QR_GRADIENT):
            await ctx.send(
                embed=discord.Embed(
                    description="Failed to generate QR code.",
                    color=discord.Color.red(),
                )
            )
            return

        await self._send_qr(
            ctx,
            filename=filename,
            title="UPI QR Code",
            color=0x00FFFF,
            fields=[
                ("UPI ID", upi_value),
                ("Amount", f"₹{amount}" if amount else "Not provided"),
                ("Note", note if note else "None"),
            ],
        )

    @qr_group.command(name="paypal", description="Create QR for PayPal", aliases=["pp"])
    async def paypal_qr(self, ctx: commands.Context, ppid: str) -> None:

        if len(ppid) > 50:
            await ctx.send(
                embed=discord.Embed(
                    description="PayPal ID too long. Please limit to 50 characters.",
                    color=discord.Color.red(),
                ),
                delete_after=5,
                ephemeral=True,
            )
            return

        ppid_value = ppid.strip()
        if "@" in ppid_value:
            url = f"https://www.paypal.com/paypalme/send?recipient={ppid_value}"
        else:
            url = f"https://www.paypal.me/{ppid_value}"

        filename = self._qr_path(ctx, "paypal")
        if not generate_qr(url, filename, fill_gradient=QR_GRADIENT):
            await ctx.send(
                embed=discord.Embed(
                    description="Failed to generate QR code.",
                    color=discord.Color.red(),
                )
            )
            return

        await self._send_qr(
            ctx,
            filename=filename,
            title="PayPal QR Code",
            color=discord.Color.blue(),
            fields=[("PayPal", ppid_value)],
        )

    @qr_group.command(name="url", description="Create a QR with custom URL", aliases=["link"])
    async def qr_url(self, ctx: commands.Context, *, url: str) -> None:

        filename = self._qr_path(ctx, "url")
        if not generate_qr(url, filename, fill_gradient=QR_GRADIENT):
            await ctx.send(
                embed=discord.Embed(
                    description="Failed to generate QR code.",
                    color=discord.Color.red(),
                )
            )
            return

        await self._send_qr(
            ctx,
            filename=filename,
            title="URL QR Code",
            description=f"**URL:** {url}",
            color=discord.Color.blurple(),
        )

    @qr_group.command(name="esewa", description="Create a QR for eSewa", aliases=["npr"])
    async def qr_esewa(
        self,
        ctx: commands.Context,
        number: str,
        *,
        fullname: str,
    ) -> None:

        number_value = number.strip()
        fullname_value = fullname.strip()
        qr_data = f'{{"eSewa_id":"{number_value}","name":"{fullname_value}"}}'

        filename = self._qr_path(ctx, "esewa")
        if not generate_qr(qr_data, filename, fill_gradient=QR_GRADIENT):
            await ctx.send(
                embed=discord.Embed(
                    description="Failed to generate QR code.",
                    color=discord.Color.red(),
                )
            )
            return

        await self._send_qr(
            ctx,
            filename=filename,
            title="eSewa QR Code",
            description=f"**Name:** {fullname_value}\n**Number:** {number_value}",
            color=discord.Color.dark_green(),
        )

    @commands.hybrid_command(name="thumbnail", description="Fetch the youtube video thumnail.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(link="The YouTube video URL to fetch the thumbnail from.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def thumbnail(self, ctx: commands.Context, link: str) -> None:
        """
        Fetch the youtube video thumnail.
        """
        if len(link) > 100:
            await ctx.reply(
                embed=discord.Embed(
                    description="Link too long.",
                    color=discord.Color.red(),
                ),
                delete_after=5,
                ephemeral=True,
            )
            return
        try:
            pattern = r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|embed/))([\w-]{11})"
            match = re.search(pattern, link)
            if not match:
                raise ValueError("Invalid YouTube URL.")
            vid_id = match.group(1)
            thumb_url = f"https://img.youtube.com/vi/{vid_id}/maxresdefault.jpg"
            embed = discord.Embed(description=f"## [YouTube Thumbnail]({link})", color=discord.Color.red())
            embed.set_image(url=thumb_url)
            await ctx.reply(embed=embed, mention_author=False)
        except Exception:
            embed = discord.Embed(description=" ❕ Error Fetching Thumbnail", color=discord.Color.red())
            await ctx.reply(embed=embed, mention_author=False, delete_after=5, ephemeral=True)

    @commands.hybrid_command(
        name="instagram",
        description="Watch an Instagram reel without leaving Discord.",
        aliases=["ig"],
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(link="The Instagram reel URL to embed.")
    async def igv(self, ctx: commands.Context, link: str) -> None:
        """
        Embed an Instagram reel or post.
        """
        pattern = r"(?:https?://)?(?:www\.)?instagram\.com/(?:reel|p)/([a-zA-Z0-9_-]+)/?"
        match = re.search(pattern, link)
        if not match:
            await ctx.reply("Invalid Instagram URL.", mention_author=False, delete_after=5, ephemeral=True)
            return

        newlink = link.replace("instagram.com", "kkinstagram.com")
        await ctx.reply(f"[Video]({newlink})", mention_author=False)

    @commands.hybrid_command(
        name="twitter",
        description="Watch an X.com post without leaving Discord.",
        aliases=["tweet"],
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(link="The tweet URL to embed.")
    async def twitter(self, ctx: commands.Context, link: str) -> None:
        """
        Embed an X.com post.
        """
        pattern = r"(?:https?://)?(?:www\.)?x\.com/([a-zA-Z0-9_-]+)/status/(\d+)/?"
        match = re.search(pattern, link)
        if not match:
            await ctx.reply("Invalid X.com URL.", mention_author=False, delete_after=5, ephemeral=True)
            return

        newlink = link.replace("x.com", "fixupx.com")
        await ctx.reply(f"[Post]({newlink})", mention_author=False)

    @commands.hybrid_command(name="tiktok", description="Watch a TikTok video without leaving Discord.", aliases=["tt"])
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(link="The TikTok video URL to embed.")
    async def tiktok(self, ctx: commands.Context, link: str) -> None:
        """
        Embed a TikTok video.
        """
        pattern = r"(?:https?://)?(?:www\.)?tiktok\.com/@([a-zA-Z0-9_-]+)/video/(\d+)/?"
        match = re.search(pattern, link)
        if not match:
            await ctx.reply("Invalid TikTok URL.", mention_author=False, delete_after=5, ephemeral=True)
            return

        newlink = link.replace("tiktok.com", "tnktok.com")
        await ctx.reply(f"[Video]({newlink})", mention_author=False)

    @commands.hybrid_command(name="tinyurl", description="Shorten a URL using TinyURL")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 15, commands.BucketType.user)
    @commands.max_concurrency(20, commands.BucketType.default, wait=True)
    async def shorten(self, ctx: commands.Context, url: str) -> None:
        """
        Shorten a URL using TinyURL
        """
        if len(url) > 150:
            await ctx.reply(
                embed=discord.Embed(
                    description="URL too long. Please limit to 150 characters.",
                    color=discord.Color.red(),
                ),
                delete_after=5,
                ephemeral=True,
            )
            return
        url = url.lower()
        try:
            api_url = f"https://tinyurl.com/api-create.php?url={url}"
            async with self.aiohttp.get(api_url) as resp:
                if resp.status != 200:
                    await ctx.reply(
                        "Failed to shorten the URL.",
                        ephemeral=True,
                        delete_after=5,
                    )
                    return
                short_url = await resp.text()
            embed = discord.Embed(title="Shortened URL", description=f"{short_url}", color=discord.Color.blue())
            embed.add_field(name="Original", value=url.lower(), inline=False)
            await ctx.reply(embed=embed, mention_author=False)
        except Exception as e:
            await ctx.reply("An error occurred.", ephemeral=True, delete_after=5)
            log_exception(e)

    @commands.hybrid_command(name="math", description="Perform a math calculation.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.max_concurrency(10, commands.BucketType.default, wait=True)
    @app_commands.describe(expression="The mathematical expression to evaluate.")
    async def math(self, ctx: commands.Context, *, expression: str) -> None:
        """Performs a mathematical calculation."""
        # A very basic and somewhat unsafe way to do math.
        # For a production bot, consider using a library like 'numexpr'
        try:
            # We will use a whitelist of allowed characters for security
            allowed_chars = "0123456789+-*/(). "
            if not all(char in allowed_chars for char in expression):
                await ctx.send("Invalid characters in expression.")
                return

            result = simple_eval(expression)
            embed = discord.Embed(title="Math Calculation", color=discord.Color.purple())
            embed.add_field(name="Expression", value=_code_block(expression), inline=False)
            embed.add_field(name="Result", value=_code_block(str(result)), inline=False)
            await ctx.send(embed=embed)
        except Exception:
            await ctx.send("An error occurred")

    @commands.hybrid_command(name="binary", description="Convert text to binary or binary to text.")
    @app_commands.describe(message="The text or binary to convert.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def binary(self, ctx: commands.Context, *, message: str) -> None:
        """
        Converts text to binary or binary to text.
        The command automatically detects the input type.
        """
        # Check if the input is binary or text
        if all(c in "01 " for c in message):
            # Binary to Text
            binary_values = message.split()
            ascii_string = ""
            for binary_value in binary_values:
                an_integer = int(binary_value, 2)
                ascii_character = chr(an_integer)
                ascii_string += ascii_character

            if len(ascii_string) > 2000:
                await ctx.send("The output is too long to be sent in a message.")
                return

            embed = discord.Embed(title="Binary to Text Conversion", color=0xADD8E6)
            embed.add_field(name=" Input (Binary)", value=_code_block(message), inline=False)
            embed.add_field(name=" Output (Text)", value=_code_block(ascii_string), inline=False)
            await ctx.send(embed=embed)

        else:
            binary_result = " ".join(format(ord(char), "08b") for char in message)

            if len(binary_result) > 2000:
                await ctx.send("The output is too long to be sent in a message.")
                return

            embed = discord.Embed(title="Text to Binary Conversion", color=discord.Color.blue())
            embed.add_field(
                name="Input (Text)",
                value=_code_block(message),
                inline=False,
            )
            embed.add_field(
                name="Output (Binary)",
                value=_code_block(binary_result),
                inline=False,
            )
            await ctx.send(embed=embed)

    async def translate_to_english(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        text = message.content.strip()
        if not text:
            await interaction.response.send_message(
                "This message has no text to translate.",
                ephemeral=True,
            )
            return
        try:
            translated = await self.translator.translate(text, dest="en")
            source_lang = LANGUAGES.get(translated.src, "Unknown")
            embed = discord.Embed(
                title="Translation to English",
                color=discord.Color.gold(),
            )
            embed.add_field(
                name=f"Original ({source_lang.title()})",
                value=_code_block(text),
                inline=False,
            )
            embed.add_field(
                name="Translated (English)",
                value=_code_block(translated.text),
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            await interaction.response.send_message(
                "An error occurred during translation",
                ephemeral=True,
            )

    @commands.hybrid_command(name="translate", description="Translate text to a target language.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        to="Target language (name or code, e.g., Spanish or es). Defaults to English.",
        text="The text to translate.",
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.max_concurrency(50, commands.BucketType.default, wait=True)
    async def translate(self, ctx: commands.Context, to: str = None, *, text: str) -> None:
        """Translates the given text to the requested language."""
        target = to or "en"
        resolved = _resolve_language(target)
        if resolved is None:
            await ctx.send(f"Unknown language `{target}`. Use a language name or code (e.g., `Spanish` or `es`).")
            return
        dest_code, dest_name = resolved
        try:
            # The translator can sometimes be unreliable, so we wrap it in a try-except
            translated = await self.translator.translate(text, dest=dest_code)
            source_lang = LANGUAGES.get(translated.src, "Unknown")

            embed = discord.Embed(
                title=f"Translation to {dest_name.title()}",
                color=discord.Color.gold(),
            )
            embed.add_field(
                name=f"Original ({source_lang.title()})",
                value=_code_block(text),
                inline=False,
            )
            embed.add_field(
                name=f"Translated ({dest_name.title()})",
                value=_code_block(translated.text),
                inline=False,
            )
            await ctx.send(embed=embed)
        except Exception as e:
            # await ctx.send("An error occurred during translation")
            await ctx.send(str(e))


    @commands.hybrid_command(name="deco", description="Get a user avatar decoration.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(user="The user whose avatar decoration you want to see.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.max_concurrency(50, commands.BucketType.default, wait=True)
    async def deco(self, ctx: commands.Context, user: discord.User | None = None) -> None:
        """Get a user avatar decoration."""
        target = user or ctx.author
        decoration = target.avatar_decoration
        if not decoration:
            await ctx.send(f"{target} does not have an avatar decoration.")
            return
        await ctx.send(f"{Emoji.INVITE.value} [Link of decoration](https://discord.com/shop#itemSkuId={target.avatar_decoration_sku_id})\n\n{target.avatar_decoration.url}")


    @commands.hybrid_command(name="enlarge", description="Enlarge a custom emoji.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(emoji="The custom emoji you want to enlarge.")
    async def enlarge(self, ctx: commands.Context, emoji: discord.PartialEmoji) -> None:
        """Enlarge a custom emoji to download it or see it in high resolution."""

        # Native Unicode emojis (like 😂) don't have an ID or a Discord CDN URL
        if emoji.id is None:
            await ctx.send(
                "⚠ Native Unicode emojis cannot be enlarged. Please provide a custom server emoji.",
                ephemeral=True,
            )
            return

        # Use the native .url attribute provided by discord.py (handles gif/png automatically)
        # size=1024 fetches the highest quality image available
        url = emoji.url

        embed = discord.Embed(
            description=f"Enlarged Emoji: [{emoji.name}]({url})",
            color=discord.Color.magenta(),
            timestamp=datetime.now() # Used timezone-aware datetime to prevent discord.py deprecation warnings
        )
        embed.set_image(url=url)
        embed.set_footer(text=f"Emoji ID: {emoji.id}")

        await ctx.send(embed=embed)

async def setup(bot: Amenity) -> None:
    await bot.add_cog(UserUtility(bot))
