from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import io
import os
import re
import struct
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import SplitResult, parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageOps

from api.http import close_http_session, create_http_session
from api.log import log_exception
from api.paginator import EmbedPaginator, PaginatorHelper

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.amenity import Amenity

STROKE_SIZE = 5
MAX_TEXT_OUTPUT = 1900
MAX_SAY_MESSAGE = 2000
MAX_SAY_EMBED_DESCRIPTION = 4096
TRACKING_PARAMS = {
    "_branch_match_id",
    "_branch_referrer",
    "__s",
    "action_object_map",
    "action_ref_map",
    "action_type_map",
    "adgroupid",
    "adid",
    "ascsubtag",
    "campaignid",
    "campid",
    "ck_subscriber_id",
    "creative",
    "creativeid",
    "dclid",
    "dicbo",
    "dm_i",
    "ef_id",
    "fbclid",
    "feature",
    "gclid",
    "gbraid",
    "hsctatracking",
    "igsh",
    "igshid",
    "irclickid",
    "itm_campaign",
    "itm_content",
    "itm_medium",
    "itm_source",
    "itm_term",
    "mc",
    "mc_cid",
    "mc_eid",
    "mibextid",
    "mkcid",
    "mkevt",
    "mkrid",
    "mkwid",
    "mkt_tok",
    "msclkid",
    "oly_anon_id",
    "oly_enc_id",
    "pp",
    "ref",
    "ref_",
    "ref_src",
    "ref_url",
    "referer",
    "s",
    "si",
    "soc_src",
    "soc_trk",
    "spm",
    "sr_share",
    "tag",
    "trk",
    "trkcampaign",
    "twclid",
    "ved",
    "vero_conv",
    "vero_id",
    "wbraid",
    "yclid",
}
TRACKING_PREFIXES = (
    "_hs",
    "fb_",
    "ga_",
    "hsa_",
    "matomo_",
    "mtm_",
    "pk_",
    "sc_",
    "utm_",
)
SESSION_PARAMS = {
    "jsessionid",
    "phpsessid",
    "session",
    "sessionid",
    "sid",
}
REDIRECT_PARAMS = ("url", "u", "q", "target", "redirect", "redirect_url", "redirect_uri", "to")
CONTENT_PARAMS_BY_HOST = {
    "youtu.be": {"list", "t"},
    "youtube.com": {"v", "list", "t"},
    "www.youtube.com": {"v", "list", "t"},
    "m.youtube.com": {"v", "list", "t"},
    "music.youtube.com": {"v", "list", "t"},
}
REDIRECT_HOST_PARTS = (
    "facebook.com",
    "google.",
    "instagram.com",
    "linkedin.com",
    "outlook.",
    "safelinks.protection.",
)
QUERY_LINKS = {
    "google": "https://www.google.com/search?q={query}",
    "google-images": "https://www.google.com/search?tbm=isch&q={query}",
    "youtube": "https://www.youtube.com/results?search_query={query}",
    "twitter": "https://x.com/search?q={query}",
    "chatgpt": "https://chatgpt.com/?q={query}",
    "grok": "https://grok.com/?q={query}",
    "duckduckgo": "https://duckduckgo.com/?q={query}",
    "bing": "https://www.bing.com/search?q={query}",
    "reddit": "https://www.reddit.com/search/?q={query}",
    "github": "https://github.com/search?q={query}",
    "stackoverflow": "https://stackoverflow.com/search?q={query}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={query}",
    "maps": "https://www.google.com/maps/search/{query}",
}
MORSE_CODE = {
    "A": ".-",
    "B": "-...",
    "C": "-.-.",
    "D": "-..",
    "E": ".",
    "F": "..-.",
    "G": "--.",
    "H": "....",
    "I": "..",
    "J": ".---",
    "K": "-.-",
    "L": ".-..",
    "M": "--",
    "N": "-.",
    "O": "---",
    "P": ".--.",
    "Q": "--.-",
    "R": ".-.",
    "S": "...",
    "T": "-",
    "U": "..-",
    "V": "...-",
    "W": ".--",
    "X": "-..-",
    "Y": "-.--",
    "Z": "--..",
    "0": "-----",
    "1": ".----",
    "2": "..---",
    "3": "...--",
    "4": "....-",
    "5": ".....",
    "6": "-....",
    "7": "--...",
    "8": "---..",
    "9": "----.",
    ".": ".-.-.-",
    ",": "--..--",
    "?": "..--..",
    "'": ".----.",
    "!": "-.-.--",
    "/": "-..-.",
    "(": "-.--.",
    ")": "-.--.-",
    "&": ".-...",
    ":": "---...",
    ";": "-.-.-.",
    "=": "-...-",
    "+": ".-.-.",
    "-": "-....-",
    "_": "..--.-",
    '"': ".-..-.",
    "$": "...-..-",
    "@": ".--.-.",
}
REVERSE_MORSE_CODE = {value: key for key, value in MORSE_CODE.items()}


def _code_block(value: str) -> str:
    escaped = value.replace("```", "`\u200b``")
    return f"```\n{escaped}\n```"


def _normalize_crypto_method(method: str) -> str:
    normalized = method.strip().lower().replace("_", "-")
    aliases = {
        "base64": "base64",
        "b64": "base64",
        "base-64": "base64",
        "morse": "morse",
        "moss": "morse",
        "moss-code": "morse",
        "morse-code": "morse",
        "secret": "secret-key",
        "key": "secret-key",
        "secretkey": "secret-key",
        "secret-key": "secret-key",
    }
    if normalized not in aliases:
        raise ValueError("Unsupported method. Use base64, morse, or secret-key.")
    return aliases[normalized]


def _morse_encode(text: str) -> str:
    words = []
    for word in text.upper().split(" "):
        if not word:
            continue
        letters = [MORSE_CODE[char] for char in word if char in MORSE_CODE]
        if letters:
            words.append(" ".join(letters))
    if not words:
        raise ValueError("Text contains no Morse-supported characters.")
    return " / ".join(words)


def _morse_decode(text: str) -> str:
    words = []
    for word in re.split(r"\s*/\s*", text.strip()):
        if not word:
            continue
        letters = []
        for code in word.split():
            if code not in REVERSE_MORSE_CODE:
                raise ValueError(f"Invalid Morse code: {code}")
            letters.append(REVERSE_MORSE_CODE[code])
        words.append("".join(letters))
    if not words:
        raise ValueError("No Morse code found.")
    return " ".join(words)


def _xor_with_key(data: bytes, key: str) -> bytes:
    if not key:
        raise ValueError("A secret key is required for secret-key mode.")

    key_bytes = key.encode("utf-8")
    output = bytearray()
    counter = 0
    while len(output) < len(data):
        output.extend(hashlib.sha256(key_bytes + counter.to_bytes(8, "big")).digest())
        counter += 1
    return bytes(value ^ output[index] for index, value in enumerate(data))


def _encrypt_text(method: str, text: str, key: str | None = None) -> str:
    method = _normalize_crypto_method(method)
    if method == "base64":
        return base64.b64encode(text.encode("utf-8")).decode("ascii")
    if method == "morse":
        return _morse_encode(text)
    encrypted = _xor_with_key(text.encode("utf-8"), key or "")
    return base64.urlsafe_b64encode(encrypted).decode("ascii")


def _decrypt_text(method: str, text: str, key: str | None = None) -> str:
    method = _normalize_crypto_method(method)
    if method == "base64":
        return base64.b64decode(text.encode("ascii"), validate=True).decode("utf-8")
    if method == "morse":
        return _morse_decode(text)
    encrypted = base64.urlsafe_b64decode(text.encode("ascii"))
    return _xor_with_key(encrypted, key or "").decode("utf-8")


def _totp_code(secret: str, *, interval: int = 30, digits: int = 6) -> tuple[str, int, int]:
    normalized = re.sub(r"\s+", "", secret).upper()
    normalized += "=" * (-len(normalized) % 8)
    key = base64.b32decode(normalized, casefold=True)
    now = int(time.time())
    counter = now // interval
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    resets_in = interval - (now % interval)
    return str(value % (10 ** digits)).zfill(digits), resets_in, now + resets_in


def _host_without_port(netloc: str) -> str:
    return netloc.rsplit("@", maxsplit=1)[-1].split(":", maxsplit=1)[0].lower()


def _is_tracking_param(key: str, host: str) -> bool:
    lower_key = key.lower()
    keep_params = CONTENT_PARAMS_BY_HOST.get(host, set())
    if lower_key in keep_params:
        return False
    return (
        lower_key in TRACKING_PARAMS
        or lower_key in SESSION_PARAMS
        or any(lower_key.startswith(prefix) for prefix in TRACKING_PREFIXES)
    )


def _redirect_target(parsed: SplitResult) -> str | None:
    host = _host_without_port(parsed.netloc)
    is_known_redirector = any(part in host for part in REDIRECT_HOST_PARTS)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    for key, value in params:
        if key.lower() not in REDIRECT_PARAMS:
            continue
        target = value.strip()
        target_parsed = urlsplit(target)
        if target_parsed.scheme in {"http", "https"} and target_parsed.netloc:
            if is_known_redirector or len(params) <= 2:
                return target
    return None


def _clean_path(path: str) -> tuple[str, list[str]]:
    removed: list[str] = []
    clean_path = re.sub(r";(?:jsessionid|phpsessid|sid|sessionid)=[^/?#;]*", "", path, flags=re.IGNORECASE)
    if clean_path != path:
        removed.append("path session id")

    before_ref_cleanup = clean_path
    clean_path = re.sub(r"/ref=[^/?#]*", "", clean_path, flags=re.IGNORECASE)
    if clean_path != before_ref_cleanup:
        removed.append("path ref")
    return clean_path, removed


def _sanitize_url(url: str) -> tuple[str, list[str]]:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Please provide a valid http or https URL.")

    removed: list[str] = []
    for _ in range(3):
        target = _redirect_target(parsed)
        if target is None:
            break
        removed.append("redirect wrapper")
        parsed = urlsplit(target)

    host = _host_without_port(parsed.netloc)
    clean_path, path_removed = _clean_path(parsed.path)
    removed.extend(path_removed)

    kept_params: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        value_parsed = urlsplit(value.strip())
        if _is_tracking_param(key, host):
            removed.append(key)
            continue
        if key.lower() in REDIRECT_PARAMS and value_parsed.scheme in {"http", "https"} and value_parsed.netloc:
            removed.append(key)
            continue
        kept_params.append((key, value))

    clean_query = urlencode(kept_params, doseq=True)
    fragment = parsed.fragment
    if fragment.lower().startswith(":~:text="):
        fragment = ""
        removed.append("text fragment")

    return urlunsplit((parsed.scheme, parsed.netloc, clean_path, clean_query, fragment)), removed


def _extract_user_ids(users: str) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for match in re.finditer(r"<@!?(\d{15,25})>|(?<!\d)(\d{15,25})(?!\d)", users):
        raw_id = match.group(1) or match.group(2)
        user_id = int(raw_id)
        if user_id in seen:
            continue
        ids.append(user_id)
        seen.add(user_id)
    return ids


def _html_preview_url(url: str) -> str:
    return f"http://htmlpreview.github.io/?{url}"


def _github_token() -> str | None:
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        return token.strip() or None
    return None


def _has_avatar_decoration(user: discord.User) -> bool:
    return bool(
        getattr(user, "avatar_decoration", None)
        or getattr(user, "avatar_decoration_sku_id", None)
        or getattr(user, "avatar_decoration_data", None)
    )


class TOTPRefreshView(discord.ui.View):
    def __init__(self, secret: str, author_id: int) -> None:
        super().__init__(timeout=120)
        self.secret = secret
        self.author_id = author_id
        self.message: discord.Message | None = None

    def _embed(self) -> discord.Embed:
        code, resets_in, reset_at = _totp_code(self.secret)
        embed = discord.Embed(
            title="2FA Code",
            description=f"Code: `{code}`\nResets: <t:{reset_at}:R> (`{reset_at}`)\nResets in: `{resets_in}s`",
            color=discord.Color.onyx_embed(),
        )
        embed.set_footer(text="Do not share your 2FA secret or code.")
        return embed

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)

    @discord.ui.button(label="Refresh code", style=discord.ButtonStyle.secondary)
    async def refresh_code(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This 2FA panel is not yours.", ephemeral=True)
            return
        try:
            await interaction.response.edit_message(embed=self._embed(), view=self)
        except Exception as exc:
            log_exception(exc)
            await interaction.response.send_message("Failed to refresh the code.", ephemeral=True)


def add_stroke_to_avatar(
    image_bytes: bytes,
    output_path: str,
    *,
    stroke_size: int = STROKE_SIZE,
) -> bool:
    """Crop avatar to a circle with a white border on a transparent background."""
    try:
        SCALE = 4
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")

        size = max(img.width, img.height)
        square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        square.paste(img, ((size - img.width) // 2, (size - img.height) // 2))

        canvas_size = size + stroke_size * 2
        ss = canvas_size * SCALE
        ss_size = size * SCALE
        ss_stroke = stroke_size * SCALE

        big = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
        draw = ImageDraw.Draw(big)
        draw.ellipse((0, 0, ss, ss), fill="white")

        mask = Image.new("L", (ss_size, ss_size), 0)
        draw_mask = ImageDraw.Draw(mask)
        draw_mask.ellipse((0, 0, ss_size, ss_size), fill=255)

        square_big = square.resize((ss_size, ss_size), Image.LANCZOS)
        big.paste(square_big, (ss_stroke, ss_stroke), mask)

        result = big.resize((canvas_size, canvas_size), Image.LANCZOS)
        result.save(output_path, "PNG")
        return True
    except Exception as exc:
        log_exception(exc)
        return False


def rotate_avatar(
    image_bytes: bytes,
    output_path: str,
    angle: int,
) -> bool:
    """Rotate an avatar image by the given angle."""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        rotated = img.rotate(angle, expand=True, resample=Image.LANCZOS)
        rotated.save(output_path, "PNG")
        return True
    except Exception as exc:
        log_exception(exc)
        return False


def invert_image(
    image_bytes: bytes,
    output_path: str,
) -> bool:
    """Invert the colours of an image."""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        r, g, b, a = img.split()
        rgb = Image.merge("RGB", (r, g, b))
        inverted = ImageOps.invert(rgb)
        inverted.putalpha(a)
        inverted.save(output_path, "PNG")
        return True
    except Exception as exc:
        log_exception(exc)
        return False


class Tools(commands.Cog):
    display_name = "Tools"
    group_name = "Tools"

    def __init__(self, bot: Amenity) -> None:
        self.bot = bot
        self.aiohttp = create_http_session()
        self.html_preview_menu = app_commands.ContextMenu(
            name="Preview HTML",
            callback=self.preview_html_file,
            type=discord.AppCommandType.message,
        )
        self.bot.tree.add_command(self.html_preview_menu)

    def cog_unload(self) -> None:
        close_http_session(self.aiohttp, self.bot.loop)
        self.bot.tree.remove_command(
            self.html_preview_menu.name,
            type=self.html_preview_menu.type,
        )

    async def _send_text_tool_response(
        self,
        ctx: commands.Context,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
    ) -> None:
        if ctx.interaction:
            if ctx.interaction.response.is_done():
                await ctx.interaction.followup.send(content=content, embed=embed, ephemeral=True)
            else:
                await ctx.interaction.response.send_message(content=content, embed=embed, ephemeral=True)
            return
        await ctx.send(content=content, embed=embed)

    async def _create_html_gist(self, *, filename: str, content: str) -> tuple[str, str]:
        token = _github_token()
        if token is None:
            raise RuntimeError("Missing `GITHUB_TOKEN` or `GH_TOKEN` for Gist uploads.")

        payload = {
            "description": "HTML preview uploaded by Amenity",
            "public": False,
            "files": {
                filename: {
                    "content": content,
                }
            },
        }
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with self.aiohttp.post(
            "https://api.github.com/gists",
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status not in {200, 201}:
                message = data.get("message") if isinstance(data, dict) else None
                raise RuntimeError(message or "Failed to create GitHub Gist.")

        if not isinstance(data, dict):
            raise RuntimeError("GitHub returned an invalid Gist response.")

        files = data.get("files")
        if not isinstance(files, dict) or filename not in files:
            raise RuntimeError("GitHub did not return the uploaded Gist file.")

        file_data = files[filename]
        raw_url = file_data.get("raw_url") if isinstance(file_data, dict) else None
        gist_url = data.get("html_url")
        if not raw_url or not gist_url:
            raise RuntimeError("GitHub did not return a usable Gist URL.")
        return raw_url, gist_url

    async def preview_html_file(self, interaction: discord.Interaction, message: discord.Message) -> None:
        html_file = next(
            (
                attachment
                for attachment in message.attachments
                if attachment.filename.lower().endswith((".html", ".htm"))
            ),
            None,
        )
        if html_file is None:
            await interaction.response.send_message("That message has no HTML file attachment.", ephemeral=True)
            return

        if html_file.size and html_file.size > 1_000_000:
            await interaction.response.send_message(
                "HTML file must be 1 MB or smaller for preview upload.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            html_bytes = await html_file.read()
            html_content = html_bytes.decode("utf-8", errors="replace")
            raw_url, gist_url = await self._create_html_gist(
                filename=html_file.filename,
                content=html_content,
            )
        except Exception as exc:
            log_exception(exc)
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        preview_url = _html_preview_url(raw_url)
        embed = discord.Embed(
            title="HTML Preview",
            description=f"[Open preview]({preview_url})",
            color=discord.Color.onyx_embed(),
        )
        embed.add_field(name="File", value=html_file.filename, inline=False)
        embed.add_field(name="Gist", value=f"[Open gist]({gist_url})", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _say(self, ctx: commands.Context, message: str, *, in_embed: bool = False) -> None:
        message = message.strip()
        if not message:
            await ctx.reply("Please provide a message to send.", mention_author=False, ephemeral=True)
            return

        if in_embed:
            if len(message) > MAX_SAY_EMBED_DESCRIPTION:
                await ctx.reply(
                    f"Embed messages must be {MAX_SAY_EMBED_DESCRIPTION} characters or less.",
                    mention_author=False,
                    ephemeral=True,
                )
                return

            embed = discord.Embed(description=message, color=discord.Color.onyx_embed())
            await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return

        if len(message) > MAX_SAY_MESSAGE:
            await ctx.reply(
                f"Messages must be {MAX_SAY_MESSAGE} characters or less.",
                mention_author=False,
                ephemeral=True,
            )
            return

        await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_group(
        name="say",
        description="Echo a message.",
        fallback="message",
        invoke_without_command=True,
    )
    @app_commands.describe(message="The message to echo.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def say_group(self, ctx: commands.Context, *, message: str) -> None:
        await self._say(ctx, message)

    @say_group.command(name="in-embed", description="Echo a message inside an embed.")
    @app_commands.describe(message="The message to echo inside an embed.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def say_in_embed(self, ctx: commands.Context, *, message: str) -> None:
        await self._say(ctx, message, in_embed=True)

    @commands.hybrid_group(
        name="image",
        invoke_without_command=True,
        with_app_command=True,
        description="Various image manipulation tools.",
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.max_concurrency(5, commands.BucketType.default, wait=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def image_group(self, ctx: commands.Context) -> None:
        await ctx.reply(
            "Use /image add-stroke | rotate-right | rotate-left | rotate-up | rotate-down | invert",
            delete_after=5,
            ephemeral=True,
            mention_author=False,
        )

    @image_group.command(
        name="add-stroke",
        description="Add a white outline on the profile picture of selected users.",
    )
    @app_commands.describe(
        user="The user whose profile picture to add the stroke to.",
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def image_add_stroke(
        self,
        ctx: commands.Context,
        user: discord.User | None = None,
    ) -> None:
        target = user or ctx.author
        avatar_url = target.display_avatar.with_format("png").with_size(512)
        output = Path(f"stroke_{target.id}.png")

        await ctx.defer()

        try:
            async with self.aiohttp.get(str(avatar_url)) as resp:
                if resp.status != 200:
                    await ctx.send("Failed to fetch the avatar.")
                    return
                avatar_bytes = await resp.read()

            if not add_stroke_to_avatar(avatar_bytes, str(output)):
                await ctx.send("Failed to process the image.")
                return

            embed = discord.Embed(
                title=f"Stroked Avatar: {target}",
                color=discord.Color.onyx_embed()
            )
            embed.set_image(url=f"attachment://{output.name}")
            embed.set_footer(
                text=f"Requested by {ctx.author}",
                icon_url=ctx.author.display_avatar.url,
            )
            await ctx.send(embed=embed, file=discord.File(str(output)))
        except Exception as exc:
            log_exception(exc)
            await ctx.send("An error occurred while processing the image.")
        finally:
            output.unlink(missing_ok=True)

    async def _process_avatar(
        self,
        ctx: commands.Context,
        user: discord.User | None,
        process_fn: Callable[[bytes, str], bool],
        filename_prefix: str,
        embed_title: str,
    ) -> None:
        target = user or ctx.author
        avatar_url = target.display_avatar.with_format("png").with_size(512)
        output = Path(f"{filename_prefix}_{target.id}.png")

        await ctx.defer()

        try:
            async with self.aiohttp.get(str(avatar_url)) as resp:
                if resp.status != 200:
                    await ctx.send("Failed to fetch the avatar.")
                    return
                avatar_bytes = await resp.read()

            if not process_fn(avatar_bytes, str(output)):
                await ctx.send("Failed to process the image.")
                return

            embed = discord.Embed(
                title=f"{embed_title}: {target}",
                color=discord.Color.onyx_embed(),
            )
            embed.set_image(url=f"attachment://{output.name}")
            embed.set_footer(
                text=f"Requested by {ctx.author}",
                icon_url=ctx.author.display_avatar.url,
            )
            await ctx.send(embed=embed, file=discord.File(str(output)))
        except Exception as exc:
            log_exception(exc)
            await ctx.send("An error occurred while processing the image.")
        finally:
            output.unlink(missing_ok=True)

    @image_group.command(
        name="rotate-right",
        description="Rotate the profile picture 90 degrees clockwise.",
    )
    @app_commands.describe(
        user="The user whose profile picture to rotate.",
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def image_rotate_right(
        self,
        ctx: commands.Context,
        user: discord.User | None = None,
    ) -> None:
        await self._process_avatar(
            ctx, user,
            lambda b, p: rotate_avatar(b, p, -90),
            "rotate-right",
            "Rotated Right",
        )

    @image_group.command(
        name="rotate-left",
        description="Rotate the profile picture 90 degrees counter-clockwise.",
    )
    @app_commands.describe(
        user="The user whose profile picture to rotate.",
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def image_rotate_left(
        self,
        ctx: commands.Context,
        user: discord.User | None = None,
    ) -> None:
        await self._process_avatar(
            ctx, user,
            lambda b, p: rotate_avatar(b, p, 90),
            "rotate-left",
            "Rotated Left",
        )

    @image_group.command(
        name="rotate-up",
        description="Rotate the profile picture upside down.",
    )
    @app_commands.describe(
        user="The user whose profile picture to rotate.",
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def image_rotate_up(
        self,
        ctx: commands.Context,
        user: discord.User | None = None,
    ) -> None:
        await self._process_avatar(
            ctx, user,
            lambda b, p: rotate_avatar(b, p, 180),
            "rotate-up",
            "Rotated Up",
        )

    @image_group.command(
        name="rotate-down",
        description="Rotate the profile picture upside down.",
    )
    @app_commands.describe(
        user="The user whose profile picture to rotate.",
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def image_rotate_down(
        self,
        ctx: commands.Context,
        user: discord.User | None = None,
    ) -> None:
        await self._process_avatar(
            ctx, user,
            lambda b, p: rotate_avatar(b, p, 180),
            "rotate-down",
            "Rotated Down",
        )


    @image_group.command(
        name="invert",
        description="Invert the colours of an image.",
    )
    @app_commands.describe(
        image="The image to invert.",
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def image_invert(
        self,
        ctx: commands.Context,
        image: discord.Attachment,
    ) -> None:
        output = Path(f"invert_{ctx.author.id}.png")

        await ctx.defer()

        try:
            image_bytes = await image.read()

            if not invert_image(image_bytes, str(output)):
                await ctx.send("Failed to process the image.")
                return

            embed = discord.Embed(
                title="Inverted Image",
                color=discord.Color.onyx_embed(),
            )
            embed.set_image(url=f"attachment://{output.name}")
            embed.set_footer(
                text=f"Requested by {ctx.author}",
                icon_url=ctx.author.display_avatar.url,
            )
            await ctx.send(embed=embed, file=discord.File(str(output)))
        except Exception as exc:
            log_exception(exc)
            await ctx.send("An error occurred while processing the image.")
        finally:
            output.unlink(missing_ok=True)

    @commands.hybrid_command(name="encrypt", description="Encrypt or encode text.")
    @app_commands.choices(
        method=[
            app_commands.Choice(name="base64", value="base64"),
            app_commands.Choice(name="morse", value="morse"),
            app_commands.Choice(name="secret-key", value="secret-key"),
        ]
    )
    @app_commands.describe(
        method="base64, morse, or secret-key",
        text="The text to encrypt or encode.",
        key="Required when method is secret-key.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def encrypt(
        self,
        ctx: commands.Context,
        method: str,
        key: str | None = None,
        *,
        text: str,
    ) -> None:
        try:
            result = _encrypt_text(method, text, key)
        except Exception as exc:
            await self._send_text_tool_response(ctx, content=str(exc))
            return

        if len(result) > MAX_TEXT_OUTPUT:
            await self._send_text_tool_response(ctx, content="The encrypted output is too long to send.")
            return

        embed = discord.Embed(title="Encrypted Text", color=discord.Color.onyx_embed())
        embed.add_field(name="Method", value=f"`{_normalize_crypto_method(method)}`", inline=False)
        embed.add_field(name="Output", value=_code_block(result), inline=False)
        await self._send_text_tool_response(ctx, embed=embed)

    @commands.hybrid_command(name="decrypt", description="Decrypt or decode text.")
    @app_commands.choices(
        method=[
            app_commands.Choice(name="base64", value="base64"),
            app_commands.Choice(name="morse", value="morse"),
            app_commands.Choice(name="secret-key", value="secret-key"),
        ]
    )
    @app_commands.describe(
        method="base64, morse, or secret-key",
        text="The text to decrypt or decode.",
        key="Required when method is secret-key.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def decrypt(
        self,
        ctx: commands.Context,
        method: str,
        key: str | None = None,
        *,
        text: str,
    ) -> None:
        try:
            result = _decrypt_text(method, text, key)
        except Exception:
            await self._send_text_tool_response(
                ctx,
                content="Failed to decrypt. Check the method, key, and input text.",
            )
            return

        if len(result) > MAX_TEXT_OUTPUT:
            await self._send_text_tool_response(ctx, content="The decrypted output is too long to send.")
            return

        embed = discord.Embed(title="Decrypted Text", color=discord.Color.onyx_embed())
        embed.add_field(name="Method", value=f"`{_normalize_crypto_method(method)}`", inline=False)
        embed.add_field(name="Output", value=_code_block(result), inline=False)
        await self._send_text_tool_response(ctx, embed=embed)

    @commands.hybrid_command(name="2fa", description="Generate a 2FA/TOTP code from a secret.")
    @app_commands.describe(secret="The base32 2FA secret.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def two_factor_code(self, ctx: commands.Context, *, secret: str) -> None:
        try:
            view = TOTPRefreshView(secret, ctx.author.id)
            embed = view._embed()
        except Exception:
            await ctx.send("Invalid 2FA secret.")
            return

        if ctx.interaction:
            await ctx.interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            with contextlib.suppress(discord.HTTPException):
                view.message = await ctx.interaction.original_response()
            return

        view.message = await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(name="sanitize-url", description="Remove tracking parameters from a URL.")
    @app_commands.describe(url="The URL to clean.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def sanitize_url(self, ctx: commands.Context, *, url: str) -> None:
        try:
            clean_url, removed = _sanitize_url(url)
        except Exception as exc:
            await ctx.send(str(exc))
            return

        if len(clean_url) > MAX_TEXT_OUTPUT:
            await ctx.send("The sanitized URL is too long to send.")
            return

        embed = discord.Embed(title="Sanitized URL", color=discord.Color.onyx_embed())
        embed.add_field(name="URL", value=clean_url, inline=False)
        removed_text = ", ".join(dict.fromkeys(removed)) if removed else "No tracking parameters found."
        embed.add_field(name="Removed", value=removed_text[:1024], inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="query-link", description="Create a search/query link for a service.")
    @app_commands.choices(
        engine=[
            app_commands.Choice(name="Google", value="google"),
            app_commands.Choice(name="Google Images", value="google-images"),
            app_commands.Choice(name="YouTube", value="youtube"),
            app_commands.Choice(name="X/Twitter", value="twitter"),
            app_commands.Choice(name="ChatGPT", value="chatgpt"),
            app_commands.Choice(name="Grok", value="grok"),
            app_commands.Choice(name="DuckDuckGo", value="duckduckgo"),
            app_commands.Choice(name="Bing", value="bing"),
            app_commands.Choice(name="Reddit", value="reddit"),
            app_commands.Choice(name="GitHub", value="github"),
            app_commands.Choice(name="Stack Overflow", value="stackoverflow"),
            app_commands.Choice(name="Wikipedia", value="wikipedia"),
            app_commands.Choice(name="Google Maps", value="maps"),
        ]
    )
    @app_commands.describe(
        engine="The service to build a query link for.",
        query="The search text.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def query_link(self, ctx: commands.Context, engine: str, *, query: str) -> None:
        query = query.strip()
        if not query:
            await ctx.send("Please provide a query.")
            return

        template = QUERY_LINKS.get(engine)
        if template is None:
            await ctx.send("Unknown query link engine.")
            return

        url = template.format(query=quote_plus(query))
        embed = discord.Embed(title="Query Link", color=discord.Color.onyx_embed())
        embed.add_field(name="Engine", value=f"`{engine}`", inline=False)
        embed.add_field(name="URL", value=url, inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="checkusers", description="Check mentioned users for young accounts.")
    @app_commands.describe(
        users="Mention users or paste user IDs separated by spaces.",
        days="Flag accounts younger than this many days. Default: 90.",
        no_avatar="Also include users with no custom avatar.",
        deco="Also include users with an avatar decoration.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def checkusers(
        self,
        ctx: commands.Context,
        users: str,
        days: int = 90,
        no_avatar: bool = False,
        deco: bool = False,
    ) -> None:
        if days < 1 or days > 3650:
            await ctx.send("Days must be between 1 and 3650.")
            return

        user_ids = _extract_user_ids(users)
        if not user_ids:
            await ctx.send("Please mention users or paste user IDs to check.")
            return
        if len(user_ids) > 50:
            await ctx.send("Please check 50 users or fewer at once.")
            return

        await ctx.defer(ephemeral=bool(ctx.interaction))

        now = discord.utils.utcnow()
        matched_lines: list[str] = []
        failed = 0
        for user_id in user_ids:
            user = self.bot.get_user(user_id)
            if user is None:
                try:
                    user = await self.bot.fetch_user(user_id)
                except discord.HTTPException:
                    failed += 1
                    continue

            account_age_days = (now - user.created_at).days
            has_no_avatar = user.avatar is None
            has_deco = _has_avatar_decoration(user)
            is_young = account_age_days < days
            if not is_young and not (no_avatar and has_no_avatar) and not (deco and has_deco):
                continue

            avatar_text = "no avatar" if has_no_avatar else "has avatar"
            deco_text = "has deco" if has_deco else "no deco"
            created_ts = int(user.created_at.timestamp())
            reasons = []
            if is_young:
                reasons.append(f"under {days}d")
            if no_avatar and has_no_avatar:
                reasons.append("no avatar")
            if deco and has_deco:
                reasons.append("has deco")
            matched_lines.append(
                f"{user.mention} (`{user.id}`) - {account_age_days}d old, {avatar_text}, {deco_text}, "
                f"{', '.join(reasons)}, created <t:{created_ts}:R>"
            )

        title = "Checked Users"
        if not matched_lines:
            matched_lines = ["No users matched the filters."]

        embeds = PaginatorHelper.create_adaptive_embeds(
            matched_lines,
            title=title,
            items_per_page=10,
            max_chars=3000,
            color=discord.Color.onyx_embed(),
        )
        for embed in embeds:
            embed.add_field(name="Age filter", value=f"Under `{days}` days", inline=True)
            embed.add_field(name="No avatar filter", value="Enabled" if no_avatar else "Disabled", inline=True)
            embed.add_field(name="Deco filter", value="Enabled" if deco else "Disabled", inline=True)
            embed.add_field(name="Checked", value=str(len(user_ids)), inline=True)
            if failed:
                embed.add_field(name="Could not fetch", value=str(failed), inline=True)

        view = EmbedPaginator(embeds, author_id=ctx.author.id) if len(embeds) > 1 else None
        if ctx.interaction:
            await ctx.send(embed=embeds[0], view=view, ephemeral=True)
            return
        await ctx.send(embed=embeds[0], view=view)

    @commands.hybrid_command(name="replace", description="Replace text inside a message.")
    @app_commands.describe(
        from_="The text to replace.",
        to="The replacement text.",
        text="The full text to edit.",
    )
    @app_commands.rename(from_="from")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def replace_text(
        self,
        ctx: commands.Context,
        from_: str,
        to: str,
        *,
        text: str,
    ) -> None:
        if not from_:
            await ctx.send("The text to replace cannot be empty.")
            return

        result = text.replace(from_, to)
        if len(result) > MAX_TEXT_OUTPUT:
            await ctx.send("The replaced text is too long to send.")
            return

        embed = discord.Embed(title="Replaced Text", color=discord.Color.onyx_embed())
        embed.add_field(name="Output", value=_code_block(result), inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="figlet", description="Render text as figlet ASCII art.")
    @app_commands.describe(text="The text to render.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def figlettext(self, ctx: commands.Context, *, text: str) -> None:
        if len(text) > 80:
            await ctx.send("Text must be 80 characters or less.")
            return

        try:
            process = subprocess.run(
                ["figlet", text],
                capture_output=True,
                check=True,
                text=True,
                timeout=5,
            )
            result = process.stdout.rstrip()
        except Exception:
            await ctx.send("Figlet is not available on this system.")
            return

        if not result:
            await ctx.send("No figlet output generated.")
            return
        if len(result) > MAX_TEXT_OUTPUT:
            await ctx.send("The figlet output is too long to send.")
            return

        await ctx.send(_code_block(result))


async def setup(bot: Amenity) -> None:
    await bot.add_cog(Tools(bot))
