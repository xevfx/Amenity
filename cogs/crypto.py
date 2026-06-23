import asyncio
import os
from contextlib import suppress

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from api.emojis import Emoji
from api.log import log_exception
from core.amenity import Amenity
from core.cache import cache

_PRICE_CACHE_KEY = "ltc:usd_price"
_ETH_PRICE_CACHE_KEY = "eth:usd_price"
_BLOCKCYPHER_TOKEN = os.getenv("BLOCKCYPHER_TOKEN")
_SOL_PRICE_CACHE_KEY = "sol:usd_price"
_COIN_LIST_CACHE_KEY = "coingecko:coin_list"
_PRICE_DETAILS_CACHE_PREFIX = "price:multi:"
_FIAT_CODES = {
    "usd",
    "eur",
    "gbp",
    "inr",
    "aud",
    "cad",
    "chf",
    "cny",
    "jpy",
    "krw",
    "hkd",
    "sgd",
    "nzd",
    "sek",
    "nok",
    "dkk",
    "pln",
    "czk",
    "huf",
    "try",
    "mxn",
    "brl",
    "zar",
    "idr",
    "php",
    "thb",
    "myr",
    "vnd",
    "aed",
    "sar",
    "egp",
    "ngn",
    "ars",
    "clp",
    "cop",
    "pen",
    "twd",
    "uah",
    "ils",
    "ron",
    "bgn",
    "isk",
    "hrk",
    "pkr",
    "bdt",
    "lkr",
    "kes",
    "ghs",
    "mad",
    "dzd",
    "tnd",
    "qar",
    "kwd",
    "omr",
}


def _blockcypher_url(network: str, address: str) -> str:
    base = f"https://api.blockcypher.com/v1/{network}/main/addrs/{address}/balance"
    if _BLOCKCYPHER_TOKEN:
        return f"{base}?token={_BLOCKCYPHER_TOKEN}"
    return base


class Crypto(commands.Cog):
    display_name = "Crypto"
    group_name = "Utilities"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.aiohttp = aiohttp.ClientSession()

    def cog_unload(self) -> None:
        if not self.aiohttp.closed:
            self.bot.loop.create_task(self.aiohttp.close())

    def _is_fiat(self, code: str) -> bool:
        return code.lower().strip() in _FIAT_CODES

    async def _fetch_json(self, url: str) -> dict | None:
        try:
            async with self.aiohttp.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except (TimeoutError, aiohttp.ClientError):
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_exception(exc)
            return None

    async def _fetch_json_status(self, url: str) -> tuple[dict | None, int | None]:
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

    async def _get_coin_list(self) -> list[dict]:
        # Avoid caching an empty list result from CoinGecko. If the cached
        # value is an empty list it likely indicates a previous fetch failure
        # and we should attempt to call the API again instead of treating the
        # empty list as authoritative (which causes lookups to always fail).
        cache_key = _COIN_LIST_CACHE_KEY
        cached = cache.get(cache_key)
        if isinstance(cached, list) and cached:
            return cached

        url = "https://api.coingecko.com/api/v3/coins/list"
        data = await self._fetch_json(url)
        if isinstance(data, list) and data:
            cache.set(cache_key, data, ttl=21_600)
            return data
        # On failure, return an empty list but do not cache it so next call
        # will try the API again.
        return []

    async def _get_coin_id(self, coin: str) -> str | None:
        query = coin.lower().strip()
        if not query:
            return None

        aliases = {
            "btc": "bitcoin",
            "eth": "ethereum",
            "ltc": "litecoin",
            "sol": "solana",
            "bnb": "binancecoin",
            "doge": "dogecoin",
            "xrp": "ripple",
            "ada": "cardano",
            "dot": "polkadot",
        }
        if query in aliases:
            return aliases[query]

        coins = await self._get_coin_list()
        if not coins:
            return None

        # Prefer exact ID match
        for entry in coins:
            if entry.get("id") == query:
                return entry.get("id")

        # Then name match
        for entry in coins:
            if str(entry.get("name") or "").lower() == query:
                return entry.get("id")

        # Then symbol match
        for entry in coins:
            if str(entry.get("symbol") or "").lower() == query:
                return entry.get("id")

        return None

    async def _get_price_details(self, coin_id: str) -> dict | None:
        cache_key = f"{_PRICE_DETAILS_CACHE_PREFIX}{coin_id}"
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            return cached

        url = (
            "https://api.coingecko.com/api/v3/simple/price"
            f"?ids={coin_id}&vs_currencies=usd,eur,inr,gbp&include_24hr_change=true"
        )
        data = await self._fetch_json(url)
        if not data or not isinstance(data.get(coin_id), dict):
            return None

        price_data = data[coin_id]
        cache.set(cache_key, price_data, ttl=60)
        return price_data

    async def _get_ltc_price_usd(self) -> float | None:
        cached = cache.get(_PRICE_CACHE_KEY)
        if isinstance(cached, (int, float)):
            return float(cached)

        coingecko_url = "https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=usd"
        data = await self._fetch_json(coingecko_url)
        price = None
        if data and isinstance(data.get("litecoin"), dict):
            price = data["litecoin"].get("usd")

        if price is None:
            binance_url = "https://api.binance.com/api/v3/ticker/price?symbol=LTCUSDT"
            data = await self._fetch_json(binance_url)
            if data and "price" in data:
                with suppress(ValueError, TypeError):
                    price = float(data["price"])

        if price is None:
            return None

        cache.set(_PRICE_CACHE_KEY, float(price), ttl=60)
        return float(price)

    async def _get_price_usd(self, coin_id: str, cache_key: str) -> float | None:
        cached = cache.get(cache_key)
        if isinstance(cached, (int, float)):
            return float(cached)

        coingecko_url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
        data = await self._fetch_json(coingecko_url)
        price = None
        if data and isinstance(data.get(coin_id), dict):
            price = data[coin_id].get("usd")

        if price is None:
            return None

        cache.set(cache_key, float(price), ttl=60)
        return float(price)

    async def _send_embed(
        self,
        ctx: commands.Context,
        description: str,
        *,
        title: str | None = None,
        color: discord.Color = discord.Color.blurple(),
        ephemeral: bool = True,
    ) -> None:
        embed = discord.Embed(description=description, color=color)
        if title:
            embed.title = title
        if ctx.interaction:
            if ctx.interaction.response.is_done():
                await ctx.interaction.followup.send(embed=embed, ephemeral=ephemeral)
            else:
                await ctx.interaction.response.send_message(embed=embed, ephemeral=ephemeral)
            return
        await ctx.send(embed=embed)

    @commands.hybrid_group(
        name="balance",
        description="Get crypto address balances",
        invoke_without_command=True,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def balance(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @balance.command(name="ltc", description="Get the balance of a ltc address.")
    @app_commands.describe(address="The Litecoin address to check")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def ltc_balance(self, ctx: commands.Context, address: str) -> None:
        address = address.strip()
        if not address:
            await self._send_embed(ctx, "Usage: /balance ltc <address>", ephemeral=False)
            return

        url = _blockcypher_url("ltc", address)
        try:
            async with self.aiohttp.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    data = await response.json()
                elif response.status == 429:
                    await ctx.send("⚠️ Rate limited by BlockCypher. Please try again later.")
                    return
                else:
                    await ctx.send(f"Error fetching balance from BlockCypher (Status: {response.status}).")
                    return
        except Exception as exc:
            log_exception(exc)
            await ctx.send("An error occurred while fetching LTC balance.")
            return

        balance = data.get("balance", 0)
        unconfirmed_balance = data.get("unconfirmed_balance", 0)
        total_received = data.get("total_received", 0)
        n_tx = data.get("n_tx", 0)

        balance_in_coin = balance / 10**8
        unconfirmed_balance_in_coin = unconfirmed_balance / 10**8
        total_received_in_coin = total_received / 10**8

        price_usd = await self._get_ltc_price_usd()

        embed = discord.Embed(
            description=f"[{address}](https://live.blockcypher.com/ltc/address/{address})",
            color=0xB0C4DE,
        )

        balance_str = f"{balance_in_coin:.8f} LTC"
        if price_usd:
            balance_usd = balance_in_coin * price_usd
            balance_str += f" (${balance_usd:,.2f} USD)"
        embed.add_field(name="Balance", value=balance_str, inline=False)

        unconfirmed_balance_str = f"{unconfirmed_balance_in_coin:.8f} LTC"
        if price_usd:
            unconfirmed_balance_usd = unconfirmed_balance_in_coin * price_usd
            unconfirmed_balance_str += f" (${unconfirmed_balance_usd:,.2f} USD)"
        embed.add_field(
            name="Unconfirmed Balance",
            value=unconfirmed_balance_str,
            inline=False,
        )

        total_received_str = f"{total_received_in_coin:.8f} LTC"
        if price_usd:
            total_received_usd = total_received_in_coin * price_usd
            total_received_str += f" (${total_received_usd:,.2f} USD)"
        embed.add_field(name="Total Received", value=total_received_str, inline=False)

        embed.set_footer(text=f"Total Transactions: {n_tx}")
        await ctx.send(embed=embed)

    @balance.command(name="eth", description="Get the balance of an eth address.")
    @app_commands.describe(address="The Ethereum address to check")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def eth_balance(self, ctx: commands.Context, address: str) -> None:
        address = address.strip()
        if not address:
            await self._send_embed(ctx, "Usage: /balance eth <address>", ephemeral=False)
            return

        url = _blockcypher_url("eth", address)
        try:
            async with self.aiohttp.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    data = await response.json()
                elif response.status == 429:
                    await ctx.send("⚠️ Rate limited by BlockCypher. Please try again later.")
                    return
                else:
                    await ctx.send(f"Error fetching balance from BlockCypher (Status: {response.status}).")
                    return
        except Exception as exc:
            log_exception(exc)
            await ctx.send("An error occurred while fetching ETH balance.")
            return

        balance = data.get("balance", 0)
        unconfirmed_balance = data.get("unconfirmed_balance", 0)
        total_received = data.get("total_received", 0)
        n_tx = data.get("n_tx", 0)

        balance_in_coin = balance / 10**18
        unconfirmed_balance_in_coin = unconfirmed_balance / 10**18
        total_received_in_coin = total_received / 10**18

        price_usd = await self._get_price_usd("ethereum", _ETH_PRICE_CACHE_KEY)

        embed = discord.Embed(
            description=f"[{address}](https://live.blockcypher.com/eth/address/{address})",
            color=0x627EEA,
        )

        balance_str = f"{balance_in_coin:.8f} ETH"
        if price_usd:
            balance_usd = balance_in_coin * price_usd
            balance_str += f" (${balance_usd:,.2f} USD)"
        embed.add_field(name="Balance", value=balance_str, inline=False)

        unconfirmed_balance_str = f"{unconfirmed_balance_in_coin:.8f} ETH"
        if price_usd:
            unconfirmed_balance_usd = unconfirmed_balance_in_coin * price_usd
            unconfirmed_balance_str += f" (${unconfirmed_balance_usd:,.2f} USD)"
        embed.add_field(
            name="Unconfirmed Balance",
            value=unconfirmed_balance_str,
            inline=False,
        )

        total_received_str = f"{total_received_in_coin:.8f} ETH"
        if price_usd:
            total_received_usd = total_received_in_coin * price_usd
            total_received_str += f" (${total_received_usd:,.2f} USD)"
        embed.add_field(name="Total Received", value=total_received_str, inline=False)

        embed.set_footer(text=f"Total Transactions: {n_tx}")
        await ctx.send(embed=embed)

    @balance.command(name="sol", description="Get the balance of a sol address.")
    @app_commands.describe(address="The Solana address to check")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def sol_balance(self, ctx: commands.Context, address: str) -> None:
        address = address.strip()
        if not address:
            await self._send_embed(ctx, "Usage: /balance sol <address>", ephemeral=False)
            return

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [address],
        }
        try:
            async with self.aiohttp.post(
                "https://api.mainnet-beta.solana.com",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                else:
                    await ctx.send(f"Error fetching balance from Solana RPC (Status: {response.status}).")
                    return
        except Exception as exc:
            log_exception(exc)
            await ctx.send("An error occurred while fetching SOL balance.")
            return

        result = data.get("result") or {}
        value = result.get("value")
        if value is None:
            await ctx.send("Unable to read balance from Solana RPC response.")
            return

        balance_in_coin = value / 10**9
        price_usd = await self._get_price_usd("solana", _SOL_PRICE_CACHE_KEY)

        embed = discord.Embed(
            description=f"[{address}](https://solscan.io/account/{address})",
            color=0x00FFA3,
        )

        balance_str = f"{balance_in_coin:.9f} SOL"
        if price_usd:
            balance_usd = balance_in_coin * price_usd
            balance_str += f" (${balance_usd:,.2f} USD)"
        embed.add_field(name="Balance", value=balance_str, inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="price", description="Get the price of a cryptocurrency.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(coin="The cryptocurrency to get the price of")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def crypto_price(self, ctx: commands.Context, *, coin: str) -> None:
        """
        Get the price of a cryptocurrency.
        Examples: ,price btc | ,price bitcoin | ,price eth
        """
        coin = coin.lower().strip()
        coin_id = await self._get_coin_id(coin)
        if not coin_id:
            await self._send_embed(
                ctx,
                f"Could not find a coin with the name: `{coin}`",
                color=discord.Color.red(),
                ephemeral=False,
            )
            return

        coin_data = await self._get_price_details(coin_id)
        if not coin_data:
            await self._send_embed(
                ctx,
                f"Could not fetch price data for `{coin}`",
                color=discord.Color.red(),
                ephemeral=False,
            )
            return

        def format_line(symbol: str, value: float | int | None) -> str | None:
            if value is None:
                return None
            value = float(value)
            if value >= 0.01:
                return f"{symbol}{value:,.2f}"
            return f"{symbol}{value:.8f}"

        lines = [
            format_line("$", coin_data.get("usd")),
            format_line("€", coin_data.get("eur")),
            format_line("£", coin_data.get("gbp")),
            format_line("₹", coin_data.get("inr")),
        ]
        lines = [line for line in lines if line]
        if not lines:
            await self._send_embed(
                ctx,
                f"Could not fetch price data for `{coin}`",
                color=discord.Color.red(),
                ephemeral=False,
            )
            return

        change_24h = coin_data.get("usd_24h_change")
        change_str = ""
        if isinstance(change_24h, (int, float)):
            emoji = "📈" if change_24h >= 0 else "📉"
            change_str = f"\n\n{emoji} **24h Change:** {change_24h:+.2f}%"

        embed = discord.Embed(
            title=f"{Emoji.CRYPTO.value} Price of {coin.upper()}",
            description=" | ".join(lines) + change_str,
            color=discord.Color.green()
            if (isinstance(change_24h, (int, float)) and change_24h >= 0)
            else discord.Color.gold(),
        )
        embed.set_footer(text="Data from CoinGecko")
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="convert",
        description="Convert between currencies (crypto & fiat).",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    @app_commands.describe(
        amount="The amount to convert",
        fromm="The currency to convert from",
        to="The currency to convert to",
    )
    async def crypto_convert(
        self,
        ctx: commands.Context,
        amount: float,
        fromm: str,
        to: str,
    ) -> None:
        """
        Convert between any currencies (crypto & fiat).
        Examples:
        - ,convert 100 usd inr (fiat to fiat)
        - ,convert 1 btc usd (crypto to fiat)
        - ,convert 500 usd btc (fiat to crypto)
        - ,convert 1 btc eth (crypto to crypto)
        """
        fromm = fromm.lower().strip()
        to = to.lower().strip()

        if fromm == to:
            await ctx.send(f"{amount:,.2f} {fromm.upper()} = {amount:,.2f} {to.upper()}")
            return

        from_is_fiat = self._is_fiat(fromm)
        to_is_fiat = self._is_fiat(to)

        try:
            if from_is_fiat and to_is_fiat:
                url = f"https://api.frankfurter.app/latest?amount={amount}&from={fromm.upper()}&to={to.upper()}"
                data, status = await self._fetch_json_status(url)
                rates = data.get("rates", {}) if isinstance(data, dict) else {}
                result = rates.get(to.upper())
                if result is not None:
                    embed = discord.Embed(
                        title="Currency Conversion",
                        description=(f"**{amount:,.2f} {fromm.upper()}** = **{result:,.2f} {to.upper()}**"),
                        color=discord.Color.blue(),
                    )
                    embed.set_footer(text="Exchange rates from Frankfurter (ECB)")
                    await ctx.send(embed=embed)
                elif status == 404:
                    await ctx.send(f"Invalid currency code: `{fromm.upper()}` or `{to.upper()}`")
                else:
                    await ctx.send(f"Could not convert {fromm.upper()} to {to.upper()}.")
                return

            if not from_is_fiat and to_is_fiat:
                coin_id = await self._get_coin_id(fromm)
                if not coin_id:
                    await ctx.send(f"Could not find crypto: `{fromm}`")
                    return

                url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies={to}"
                data, status = await self._fetch_json_status(url)
                if status == 429:
                    await ctx.send("⚠️ Rate limited. Please try again later.")
                    return
                if data and coin_id in data and to in data[coin_id]:
                    rate = data[coin_id][to]
                    result = amount * rate
                    embed = discord.Embed(
                        title="Crypto to Fiat",
                        description=(f"**{amount:,.8g} {fromm.upper()}** = **{result:,.2f} {to.upper()}**"),
                        color=discord.Color.gold(),
                    )
                    embed.set_footer(text="Data from CoinGecko")
                    await ctx.send(embed=embed)
                else:
                    await ctx.send(f"Could not get rate for {fromm.upper()} in {to.upper()}.")
                return

            if from_is_fiat and not to_is_fiat:
                coin_id = await self._get_coin_id(to)
                if not coin_id:
                    await ctx.send(f"Could not find crypto: `{to}`")
                    return

                url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies={fromm}"
                data, status = await self._fetch_json_status(url)
                if status == 429:
                    await ctx.send("⚠️ Rate limited. Please try again later.")
                    return
                if data and coin_id in data and fromm in data[coin_id]:
                    rate = data[coin_id][fromm]
                    if not rate:
                        await ctx.send(f"Could not get rate for {to.upper()} in {fromm.upper()}.")
                        return
                    result = amount / rate
                    result_str = f"{result:,.6f}" if result >= 1 else f"{result:.8f}"
                    embed = discord.Embed(
                        title="Fiat to Crypto",
                        description=(f"**{amount:,.2f} {fromm.upper()}** = **{result_str} {to.upper()}**"),
                        color=discord.Color.gold(),
                    )
                    embed.set_footer(text="Data from CoinGecko")
                    await ctx.send(embed=embed)
                else:
                    await ctx.send(f"Could not get rate for {to.upper()} in {fromm.upper()}.")
                return

            if not from_is_fiat and not to_is_fiat:
                from_coin_id = await self._get_coin_id(fromm)
                to_coin_id = await self._get_coin_id(to)

                if not from_coin_id:
                    await ctx.send(f"Could not find crypto: `{fromm}`")
                    return
                if not to_coin_id:
                    await ctx.send(f"Could not find crypto: `{to}`")
                    return

                url = f"https://api.coingecko.com/api/v3/simple/price?ids={from_coin_id},{to_coin_id}&vs_currencies=usd"
                data, status = await self._fetch_json_status(url)
                if status == 429:
                    await ctx.send("⚠️ Rate limited. Please try again later.")
                    return
                from_usd = data.get(from_coin_id, {}).get("usd") if data else None
                to_usd = data.get(to_coin_id, {}).get("usd") if data else None

                if from_usd and to_usd:
                    result = (amount * from_usd) / to_usd
                    result_str = f"{result:,.6f}" if result >= 1 else f"{result:.8f}"
                    embed = discord.Embed(
                        title="Crypto to Crypto",
                        description=(f"**{amount:,.8g} {fromm.upper()}** = **{result_str} {to.upper()}**"),
                        color=discord.Color.purple(),
                    )
                    embed.add_field(
                        name="Rate",
                        value=f"1 {fromm.upper()} = {from_usd / to_usd:.8g} {to.upper()}",
                        inline=False,
                    )
                    embed.set_footer(text="Data from CoinGecko (via USD)")
                    await ctx.send(embed=embed)
                else:
                    await ctx.send("Could not get USD prices for one or both coins.")
                return
        except Exception as exc:
            log_exception(exc)
            await ctx.send("An error occurred during conversion.")


async def setup(bot: Amenity) -> None:
    await bot.add_cog(Crypto(bot))
