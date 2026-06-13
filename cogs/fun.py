import asyncio
import random
from urllib.parse import quote

import aiohttp
import discord
import pycountry
from discord import app_commands
from discord.ext import commands
from faker import Faker
from faker.config import AVAILABLE_LOCALES

from api.log import log_exception
from core.amenity import Amenity


class Fun(commands.Cog):
    display_name = "Fun"
    group_name = "Fun"


    def __init__(self, bot: Amenity) -> None:
        self.bot = bot
        self.aiohttp = aiohttp.ClientSession()
        self.supported_countries = {}
        for locale in AVAILABLE_LOCALES:
            parts = locale.split('_')
            if len(parts) > 1:
                country_code = parts[1].upper()
                self.supported_countries[country_code] = locale

    def cog_unload(self) -> None:
        if not self.aiohttp.closed:
            self.bot.loop.create_task(self.aiohttp.close())

    async def _fetch_json(self, url: str) -> tuple[dict | list | None, int | None]:
        try:
            async with self.aiohttp.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                status = resp.status
                if status != 200:
                    return None, status
                return await resp.json(), status
        except (TimeoutError, aiohttp.ClientError):
            return None, None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_exception(exc)
            return None, None

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
    @app_commands.allowed_installs(guilds=True, users=True)
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
    @app_commands.allowed_installs(guilds=True, users=True)
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

    @commands.hybrid_command(name="catfact", description="Get a random cat fact.")
    @app_commands.allowed_installs(guilds=True, users=True)
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

    @commands.hybrid_command(name="dogfact", description="Get a random dog fact.")
    @app_commands.allowed_installs(guilds=True, users=True)
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


    @commands.hybrid_command(name="gayrate", description="Check how gay somone is (for fun).")
    @app_commands.describe(user="Optional: The user to check. Defaults to yourself.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def gayrate(self, ctx: commands.Context, user: discord.User | None = None):
        target = user or ctx.author
        rate = random.randint(0, 100)
        await self._send_embed(
            ctx,
            description=f"{target.mention} is {rate}% gay!",
            title="Gay Rate",
            color=discord.Color.pink(),
            ephemeral=True
        )


    @commands.hybrid_command(name="faker", description="Generate a fake name and address by country name or code.")
    @app_commands.describe(country="Optional: Country name or 2-letter code (e.g. Nepal, India, US, IN)")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def faker_cmd(self, ctx: commands.Context, country: str | None = None):
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
                    description=f"Could not find a valid or supported country matching `{country}`. Using a random country instead.",
                    color=discord.Color.orange()
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

        embed = discord.Embed(
            title="📍 Generated Address Info",
            description=description,
            color=discord.Color.teal()
        )
        await ctx.send(embed=embed)


    @commands.hybrid_command(name="dadjoke", description="Get a random dad joke from icanhazdadjoke.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def dad_joke_cmd(self, ctx: commands.Context):

        async with aiohttp.ClientSession() as session:
            headers = {"Accept": "application/json"}
            async with session.get("https://icanhazdadjoke.com/", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    embed = discord.Embed(
                        title="👨 Dad Joke",
                        description=data["joke"],
                        color=discord.Color.orange()
                    )
                else:
                    embed = discord.Embed(title="❌ API Error", description="Failed to retrieve a dad joke.", color=discord.Color.red())

        await ctx.send(embed=embed)





    @commands.hybrid_command(name="fact", description="Get a random completely useless fact.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def useless_fact_cmd(self, ctx: commands.Context):

        async with aiohttp.ClientSession() as session:
            async with session.get("https://uselessfacts.jsph.pl/api/v2/facts/random?language=en") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    embed = discord.Embed(
                        title="🧠 Useless Fact",
                        description=data["text"],
                        color=discord.Color.teal()
                    )
                    embed.set_footer(text=f"Source: {data['source']}")
                else:
                    embed = discord.Embed(title="❌ API Error", description="Failed to retrieve a useless fact.", color=discord.Color.red())

        await ctx.send(embed=embed)

async def setup(bot: Amenity) -> None:
    await bot.add_cog(Fun(bot))
