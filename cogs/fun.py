import io
import random
import tempfile
from pathlib import Path
from urllib.parse import quote

import discord
import pycountry
from discord import app_commands
from discord.ext import commands
from faker import Faker
from faker.config import AVAILABLE_LOCALES
from PIL import Image, ImageDraw, ImageFont

from api.emojis import Emoji
from api.http import JsonData, close_http_session, create_http_session, fetch_json
from api.log import log_exception
from core.amenity import Amenity

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MEME_ASSETS_DIR = PROJECT_ROOT / "assets" / "memes"
COOLVETICA_FONT = PROJECT_ROOT / "assets" / "fonts" / "coolvetica" / "Coolvetica Rg.otf"
RIP_FONT = PROJECT_ROOT / "assets" / "fonts" / "coolvetica" / "Coolvetica Hv Comp.otf"


class Fun(commands.Cog):
    display_name = "Fun"
    group_name = "Fun"

    def __init__(self, bot: Amenity) -> None:
        self.bot = bot
        self.aiohttp = create_http_session()
        self.supported_countries = {}
        for locale in AVAILABLE_LOCALES:
            parts = locale.split("_")
            if len(parts) > 1:
                country_code = parts[1].upper()
                self.supported_countries[country_code] = locale

    def cog_unload(self) -> None:
        close_http_session(self.aiohttp, self.bot.loop)

    async def _fetch_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[JsonData | None, int | None]:
        return await fetch_json(self.aiohttp, url, headers=headers)

    def _format_definitions(
        self,
        meanings: list[dict],
        *,
        max_defs: int = 3,
    ) -> list[tuple[str, str]]:
        fields: list[tuple[str, str]] = []
        for meaning in meanings:
            part = meaning.get("partOfSpeech") or "Definition"
            definitions = meaning.get("definitions") or []
            lines: list[str] = []
            for index, definition in enumerate(definitions[:max_defs], start=1):
                text = definition.get("definition")
                if not text:
                    continue
                example = definition.get("example")
                if example:
                    lines.append(f"{index}. {text}\n> {example}")
                else:
                    lines.append(f"{index}. {text}")
            if lines:
                value = "\n".join(lines)
                fields.append((part.title(), value[:1000]))
        return fields

    @commands.hybrid_command(name="dictionary", description="Look up a word in the dictionary.")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(word="The word to define.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def dictionary(self, ctx: commands.Context, *, word: str) -> None:
        query = word.strip()
        if not query:
            await ctx.send("Please provide a word to look up.")
            return

        url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{quote(query)}"
        data, status = await self._fetch_json(url)
        if not data or status != 200 or not isinstance(data, list):
            await ctx.send(f"No definitions found for `{query}`.")
            return

        entry = data[0]
        title = entry.get("word", query).title()
        phonetic = entry.get("phonetic")
        meanings = entry.get("meanings") or []

        embed = discord.Embed(
            title=f"Dictionary: {title}",
            description=f"/{phonetic}/" if phonetic else None,
            color=discord.Color.blurple(),
        )
        for name, value in self._format_definitions(meanings):
            embed.add_field(name=name, value=value, inline=False)

        if not embed.fields:
            embed.description = embed.description or "No definitions available."

        embed.set_footer(text="Source: DictionaryAPI.dev")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="joke", description="Get a random joke.")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def joke(self, ctx: commands.Context) -> None:
        url = "https://official-joke-api.appspot.com/jokes/random"
        data, status = await self._fetch_json(url)
        if not data or status != 200 or not isinstance(data, dict):
            await ctx.send("I couldn't fetch a joke right now. Try again later.")
            return

        setup = data.get("setup")
        punchline = data.get("punchline")
        if not setup or not punchline:
            await ctx.send("I couldn't fetch a joke right now. Try again later.")
            return

        embed = discord.Embed(
            title="Random Joke",
            description=f"{setup}\n\n**{punchline}**",
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="cat-fact", description="Get a random cat fact.")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def catfact(self, ctx: commands.Context) -> None:
        url = "https://catfact.ninja/fact"
        data, status = await self._fetch_json(url)
        if not data or status != 200 or not isinstance(data, dict):
            await ctx.send("I couldn't fetch a cat fact right now. Try again later.")
            return

        fact = data.get("fact")
        if not fact:
            await ctx.send("I couldn't fetch a cat fact right now. Try again later.")
            return

        embed = discord.Embed(
            title="Random Cat Fact",
            description=fact,
            color=discord.Color.orange(),
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="dog-fact", description="Get a random dog fact.")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def dogfact(self, ctx: commands.Context) -> None:
        url = "https://dogapi.dog/api/v2/facts"
        data, status = await self._fetch_json(url)
        if not data or status != 200 or not isinstance(data, dict):
            await ctx.send("I couldn't fetch a dog fact right now. Try again later.")
            return

        facts = data.get("data")
        fact = None
        if isinstance(facts, list) and facts:
            attributes = facts[0].get("attributes") if isinstance(facts[0], dict) else None
            if isinstance(attributes, dict):
                fact = attributes.get("body")

        if not fact:
            await ctx.send("I couldn't fetch a dog fact right now. Try again later.")
            return

        embed = discord.Embed(
            title="Random Dog Fact",
            description=fact,
            color=discord.Color.blue(),
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="gay-rate", description="Check how gay somone is (for fun).")
    @app_commands.describe(user="Optional: The user to check. Defaults to yourself.")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def gayrate(self, ctx: commands.Context, user: discord.User | None = None) -> None:
        target = user or ctx.author
        rate = random.randint(0, 100)
        if target.id == 931347423773741097:
            rate = 0
        await ctx.send(
            embed=discord.Embed(
                description=f"{target.mention} is {rate}% gay!", title="Gay Rate", color=discord.Color.pink()
            )
        )

    @commands.hybrid_command(name="faker", description="Generate a fake name and address by country name or code.")
    @app_commands.describe(country="Optional: Country name or 2-letter code (e.g. Nepal, India, US, IN)")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def faker_cmd(self, ctx: commands.Context, country: str | None = None) -> None:
        chosen_locale = None
        display_code = ""
        display_name = ""

        # 1. Parse and validate country/code input
        if country:
            clean_input = country.strip()
            target_code = None

            # Check if input is a direct 2-letter country code
            if len(clean_input) == 2:
                target_code = clean_input.upper()
            else:
                # Attempt to look up by full country name
                try:
                    match = pycountry.countries.search_fuzzy(clean_input)[0]
                    target_code = match.alpha_2
                except LookupError:
                    pass  # Keep target_code as None if no country matches

            # Verify if the found code is natively supported by Faker
            if target_code in self.supported_countries:
                chosen_locale = self.supported_countries[target_code]
                display_code = target_code
                display_name = pycountry.countries.get(alpha_2=target_code).name
            else:
                # Direct alert using native ctx.send
                embed_err = discord.Embed(
                    title="Country Not Found",
                    description=(
                        f"Could not find a valid or supported country matching `{country}`. "
                        "Using a random country instead."
                    ),
                    color=discord.Color.orange(),
                )
                if ctx.interaction and not ctx.interaction.response.is_done():
                    await ctx.interaction.response.send_message(embed=embed_err, ephemeral=True)
                else:
                    await ctx.send(embed=embed_err)

        # 2. Fallback to random country if input is missing or wasn't found
        if not chosen_locale:
            display_code = random.choice(list(self.supported_countries.keys()))
            chosen_locale = self.supported_countries[display_code]
            try:
                display_name = pycountry.countries.get(alpha_2=display_code).name
            except Exception:
                display_name = "Unknown Region"

        # 3. Initialize localized Faker instance
        fake = Faker(chosen_locale)

        # 4. Generate structured identity data components safely
        name = fake.name()
        street = fake.street_address()
        city = fake.city()

        try:
            state = fake.state()
        except AttributeError:
            state = "N/A"

        try:
            postal_code = fake.postcode()
        except AttributeError:
            postal_code = "N/A"

        # 5. Format layout block and Embed object
        description = (
            f"🌐 **Country:** {display_name} (`{display_code}`)\n"
            f"--- \n"
            f"👤 **Name:** {name}\n"
            f"🛣️ **Street Address:** `{street}`\n"
            f"🏙️ **City:** `{city}`\n"
            f"🗺️ **State/Region:** `{state}`\n"
            f"📮 **Postal Code:** `{postal_code}`"
        )

        embed = discord.Embed(title="📍 Generated Address Info", description=description, color=discord.Color.teal())
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="dad-joke", description="Get a random dad joke from icanhazdadjoke.")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def dad_joke_cmd(self, ctx: commands.Context) -> None:
        data, status = await self._fetch_json("https://icanhazdadjoke.com/", headers={"Accept": "application/json"})
        if status == 200 and isinstance(data, dict):
            embed = discord.Embed(title="👨 Dad Joke", description=data["joke"], color=discord.Color.orange())
        else:
            embed = discord.Embed(
                title="❌ API Error",
                description="Failed to retrieve a dad joke.",
                color=discord.Color.red(),
            )

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="dark-joke", description="Get a random dark joke from the Official Joke API.")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.max_concurrency(20, commands.BucketType.default, wait=True)
    async def dark_joke_cmd(self, ctx: commands.Context) -> None:
        """
        Get a random dark joke from the Official Joke API.
        """
        try:
            url = "https://v2.jokeapi.dev/joke/Dark?type=single,twopart"
            data, status = await self._fetch_json(url)
            if status != 200 or not isinstance(data, dict):
                return await ctx.send("Could not fetch a joke at this time.")

            if data.get("type") == "single":
                joke_text = data.get("joke", "No joke found.")
            else:
                setup = data.get("setup", "")
                delivery = data.get("delivery", "")
                joke_text = f"{setup}\n\n{delivery}"

            embed = discord.Embed(
                title="Here's a dark joke for you!", description=joke_text, color=discord.Color.dark_grey()
            )
            await ctx.reply(embed=embed, mention_author=False)
        except Exception as e:
            await ctx.send("An error occurred")
            log_exception(e)

    @commands.hybrid_command(name="fact", description="Get a random completely useless fact.")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def useless_fact_cmd(self, ctx: commands.Context) -> None:
        data, status = await self._fetch_json("https://uselessfacts.jsph.pl/api/v2/facts/random?language=en")
        if status == 200 and isinstance(data, dict):
            embed = discord.Embed(
                title="🧠 Useless Fact",
                description=data["text"],
                color=discord.Color.teal(),
            )
            embed.set_footer(text=f"Source: {data['source']}")
        else:
            embed = discord.Embed(
                title=f"{Emoji.WARNING.value} API Error",
                description="Failed to retrieve a useless fact.",
                color=discord.Color.red(),
            )

        await ctx.send(embed=embed)


    @commands.hybrid_group(
        name="meme",
        invoke_without_command=True,
        with_app_command=True,
        description="Generate various meme images.",
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.max_concurrency(5, commands.BucketType.default, wait=True)
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def meme_group(self, ctx: commands.Context) -> None:
        await ctx.reply(
            "Use /meme rip | waiting | whiteboard",
            delete_after=5,
            ephemeral=True,
            mention_author=False,
        )

    @meme_group.command(
        name="rip",
        description="Put a user's avatar on a RIP tombstone.",
    )
    @app_commands.describe(user="The user to put on the tombstone.")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def meme_rip(self, ctx: commands.Context, user: discord.User) -> None:
        target = user
        avatar_url = target.display_avatar.with_format("png").with_size(128)
        output = _temp_png_path(f"rip_{target.id}_")

        await ctx.defer()

        try:
            async with self.aiohttp.get(str(avatar_url)) as resp:
                if resp.status != 200:
                    await ctx.send("Failed to fetch the avatar.")
                    return
                avatar_bytes = await resp.read()

            if not _generate_rip(avatar_bytes, str(output), target.display_name):
                await ctx.send("Failed to generate the image.")
                return

            embed = discord.Embed(
                title=f"RIP {target.display_name}",
                color=discord.Color.dark_gray(),
            )
            embed.set_image(url=f"attachment://{output.name}")
            embed.set_footer(
                text=f"Requested by {ctx.author}",
                icon_url=ctx.author.display_avatar.url,
            )
            await ctx.send(embed=embed, file=discord.File(str(output), filename=output.name))
        except Exception as exc:
            log_exception(exc)
            await ctx.send("An error occurred while processing the image.")
        finally:
            output.unlink(missing_ok=True)


    @meme_group.command(
        name="waiting",
        description="Put text on a waiting meme template.",
    )
    @app_commands.describe(text="The text to display (max 60 characters).")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def meme_waiting(self, ctx: commands.Context, *, text: str) -> None:
        text = text.strip()
        if not text:
            await ctx.send("Please provide some text.")
            return
        if len(text) > 60:
            await ctx.send("Text must be 60 characters or less.")
            return

        output = _temp_png_path(f"waiting_{ctx.author.id}_")

        await ctx.defer()

        try:
            if not _generate_waiting(str(output), text):
                await ctx.send("Failed to generate the image.")
                return

            embed = discord.Embed(
                title="Waiting",
                color=discord.Color.dark_gray(),
            )
            embed.set_image(url=f"attachment://{output.name}")
            embed.set_footer(
                text=f"Requested by {ctx.author}",
                icon_url=ctx.author.display_avatar.url,
            )
            await ctx.send(embed=embed, file=discord.File(str(output), filename=output.name))
        except Exception as exc:
            log_exception(exc)
            await ctx.send("An error occurred while processing the image.")
        finally:
            output.unlink(missing_ok=True)


    @meme_group.command(
        name="whiteboard",
        description="Put text on a whiteboard meme template.",
    )
    @app_commands.describe(text="The text to display (max 120 characters).")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def meme_whiteboard(self, ctx: commands.Context, *, text: str) -> None:
        text = text.strip()
        if not text:
            await ctx.send("Please provide some text.")
            return
        if len(text) > 120:
            await ctx.send("Text must be 120 characters or less.")
            return

        output = _temp_png_path(f"whiteboard_{ctx.author.id}_")

        await ctx.defer()

        try:
            if not _generate_whiteboard(str(output), text):
                await ctx.send("Failed to generate the image.")
                return

            embed = discord.Embed(
                title="Whiteboard",
                color=discord.Color.dark_gray(),
            )
            embed.set_image(url=f"attachment://{output.name}")
            embed.set_footer(
                text=f"Requested by {ctx.author}",
                icon_url=ctx.author.display_avatar.url,
            )
            await ctx.send(embed=embed, file=discord.File(str(output), filename=output.name))
        except Exception as exc:
            log_exception(exc)
            await ctx.send("An error occurred while processing the image.")
        finally:
            output.unlink(missing_ok=True)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue

        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            bbox = draw.textbbox((0, 0), candidate, font=font)
            if bbox[2] - bbox[0] <= max_width:
                current = candidate
                continue

            lines.extend(_split_long_line(draw, current, font, max_width))
            current = word

        lines.extend(_split_long_line(draw, current, font, max_width))
    return lines


def _temp_png_path(prefix: str) -> Path:
    with tempfile.NamedTemporaryFile(prefix=prefix, suffix=".png", delete=False) as handle:
        return Path(handle.name)


def _split_long_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    bbox = draw.textbbox((0, 0), text, font=font)
    if bbox[2] - bbox[0] <= max_width:
        return [text]

    lines: list[str] = []
    current = ""
    for char in text:
        candidate = f"{current}{char}"
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if current and bbox[2] - bbox[0] > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate

    if current:
        lines.append(current)
    return lines


def _generate_whiteboard(output_path: str, text: str) -> bool:
    try:
        base = Image.open(MEME_ASSETS_DIR / "whiteboard.png").convert("RGBA")
        draw = ImageDraw.Draw(base)

        box_x, box_y = 150, 110
        box_w, box_h = 520, 245
        line_spacing = 8

        font_size = 58
        while font_size > 14:
            font = ImageFont.truetype(str(COOLVETICA_FONT), font_size)
            lines = _wrap_text(draw, text, font, box_w)
            bboxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
            line_heights = [bbox[3] - bbox[1] for bbox in bboxes]
            text_h = sum(line_heights) + line_spacing * max(len(lines) - 1, 0)
            if text_h <= box_h:
                break
            font_size -= 2

        text_y = box_y + (box_h - text_h) // 2
        for line, bbox, line_h in zip(lines, bboxes, line_heights, strict=False):
            text_w = bbox[2] - bbox[0]
            text_x = box_x + (box_w - text_w) // 2
            draw.text((text_x, text_y - bbox[1]), line, fill=(32, 32, 32), font=font)
            text_y += line_h + line_spacing

        base.save(output_path, "PNG")
        return True
    except Exception:
        return False


def _generate_waiting(output_path: str, text: str) -> bool:
    try:
        base = Image.open(MEME_ASSETS_DIR / "waiting.jpg").convert("RGBA")
        w, h = base.size

        draw = ImageDraw.Draw(base)

        font_size = 70
        while font_size > 12:
            font = ImageFont.truetype(str(COOLVETICA_FONT), font_size)
            bbox = draw.textbbox((0, 0), text, font=font)
            if bbox[2] - bbox[0] <= w - 40:
                break
            font_size -= 2

        text_w = bbox[2] - bbox[0]
        text_x = (w - text_w) // 2
        text_y = 60 if font_size >= 30 else 25

        draw.text((text_x, text_y), text, fill="white", font=font)

        base.save(output_path, "PNG")
        return True
    except Exception:
        return False


def _generate_rip(avatar_bytes: bytes, output_path: str, name: str) -> bool:
    try:
        base = Image.open(MEME_ASSETS_DIR / "rip.jpg").convert("RGBA")
        w, h = base.size

        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
        avatar = avatar.resize((100, 100), Image.LANCZOS)

        avatar_x = (w - 100) // 2
        avatar_y = 155
        base.paste(avatar, (avatar_x, avatar_y))

        draw = ImageDraw.Draw(base)

        font_size = 30
        while font_size > 8:
            font = ImageFont.truetype(str(RIP_FONT), font_size)
            bbox = draw.textbbox((0, 0), name, font=font)
            if bbox[2] - bbox[0] <= w - 20:
                break
            font_size -= 2

        text_w = bbox[2] - bbox[0]
        text_x = (w - text_w) // 2
        text_y = avatar_y + 100 + 15

        draw.text((text_x, text_y), name, fill="black", font=font)

        base.save(output_path, "PNG")
        return True
    except Exception:
        return False


async def setup(bot: Amenity) -> None:
    await bot.add_cog(Fun(bot))
