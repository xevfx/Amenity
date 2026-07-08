import asyncio
import os
from contextlib import suppress
from datetime import datetime

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from api.crypto_networks import CRYPTO_NETWORK_BY_VALUE, CRYPTO_NETWORK_CHOICES
from api.emojis import Emoji
from api.http import JsonData, close_http_session, create_http_session, fetch_json
from api.log import log_exception
from core.amenity import Amenity
from core.cache import cache

_PRICE_CACHE_KEY = "ltc:usd_price"
_ETH_PRICE_CACHE_KEY = "eth:usd_price"
_BTC_PRICE_CACHE_KEY = "btc:usd_price"
_BLOCKCYPHER_TOKEN = os.getenv("BLOCKCYPHER_TOKEN")
_ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
_BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY", "")
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
        self.aiohttp = create_http_session()

    async def cog_load(self) -> None:
        self.addy_db_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "data/addy.db"),
        )
        os.makedirs(os.path.dirname(self.addy_db_path), exist_ok=True)
        self.addy_conn = await aiosqlite.connect(self.addy_db_path)
        await self.addy_conn.execute("""
            CREATE TABLE IF NOT EXISTS addresses (
                user_id INTEGER NOT NULL,
                coin TEXT NOT NULL,
                address TEXT NOT NULL,
                PRIMARY KEY (user_id, coin)
            )
        """)
        await self.addy_conn.commit()

    def cog_unload(self) -> None:
        close_http_session(self.aiohttp, self.bot.loop)
        task = self.bot.loop.create_task(self.addy_conn.close())
        task.add_done_callback(self._log_task_exception)

    def _log_task_exception(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log_exception(exc)

    def _is_fiat(self, code: str) -> bool:
        return code.lower().strip() in _FIAT_CODES

    async def _fetch_json(self, url: str) -> JsonData | None:
        data, _ = await fetch_json(self.aiohttp, url)
        return data

    async def _fetch_json_status(self, url: str) -> tuple[dict | None, int | None]:
        data, status = await fetch_json(self.aiohttp, url)
        if isinstance(data, dict):
            return data, status
        return None, status

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
            return query

        for entry in coins:
            if entry.get("id") == query:
                return entry.get("id")

        for entry in coins:
            name = str(entry.get("name") or "").lower()
            if name == query:
                return entry.get("id")

        for entry in coins:
            symbol = str(entry.get("symbol") or "").lower()
            if symbol == query:
                return entry.get("id")

        return query

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

    async def _get_btc_price_usd(self) -> float | None:
        cached = cache.get(_BTC_PRICE_CACHE_KEY)
        if isinstance(cached, (int, float)):
            return float(cached)

        coingecko_url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
        data = await self._fetch_json(coingecko_url)
        price = None
        if data and isinstance(data.get("bitcoin"), dict):
            price = data["bitcoin"].get("usd")

        if price is None:
            binance_url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
            data = await self._fetch_json(binance_url)
            if data and "price" in data:
                with suppress(ValueError, TypeError):
                    price = float(data["price"])

        if price is None:
            return None

        cache.set(_BTC_PRICE_CACHE_KEY, float(price), ttl=60)
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

    @balance.command(name="usdt-bep20", description="Get the USDT (BEP-20) balance of a BSC address.")
    @app_commands.describe(address="The BSC address to check")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def usdt_bep20_balance(self, ctx: commands.Context, address: str) -> None:
        address = address.strip()
        if not address:
            await self._send_embed(ctx, "Usage: /balance usdt-bep20 <address>", ephemeral=False)
            return

        contract = "0x55d398326f99059fF775485246999027B3197955"
        url = f"https://api.bscscan.com/api?module=account&action=tokenbalance&contractaddress={contract}&address={address}"
        if _BSCSCAN_API_KEY:
            url += f"&apikey={_BSCSCAN_API_KEY}"

        data = await self._fetch_json(url)
        if not data or data.get("status") != "1":
            await ctx.send(f"Unable to fetch USDT balance for `{address}`.")
            return

        try:
            balance_raw = int(data.get("result", "0"))
        except (ValueError, TypeError):
            await ctx.send(f"Unable to fetch USDT balance for `{address}`.")
            return

        balance = balance_raw / 10**18
        price_usd = await self._get_price_usd("tether", "usdt:usd_price")

        embed = discord.Embed(
            description=f"[{address}](https://bscscan.com/address/{address})",
            color=0xF0B90B,
        )

        balance_str = f"{balance:,.2f} USDT"
        if price_usd:
            balance_str += f" (${balance:,.2f} USD)"
        embed.add_field(name="Balance", value=balance_str, inline=False)
        await ctx.send(embed=embed)

    # ── /txid group ─────────────────────────────────────────────────

    @commands.hybrid_group(
        name="txid",
        description="Get information about a blockchain transaction",
        invoke_without_command=True,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def txid(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @txid.command(name="ltc", description="Get information about a Litecoin transaction.")
    @app_commands.describe(tx_hash="The Litecoin transaction hash (TXID)")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def ltc_txid(self, ctx: commands.Context, tx_hash: str) -> None:
        tx_hash = tx_hash.strip()
        if not tx_hash:
            await self._send_embed(ctx, "Usage: /txid ltc <tx_hash>", ephemeral=False)
            return

        url = f"https://api.blockcypher.com/v1/ltc/main/txs/{tx_hash}"
        if _BLOCKCYPHER_TOKEN:
            url += f"?token={_BLOCKCYPHER_TOKEN}"

        try:
            async with self.aiohttp.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    data = await response.json()
                elif response.status == 404:
                    await ctx.send(f"Transaction `{tx_hash}` not found on Litecoin network.")
                    return
                elif response.status == 429:
                    await ctx.send("⚠️ Rate limited by BlockCypher. Please try again later.")
                    return
                else:
                    await ctx.send(f"Error fetching transaction from BlockCypher (Status: {response.status}).")
                    return
        except Exception as exc:
            log_exception(exc)
            await ctx.send("An error occurred while fetching the transaction.")
            return

        price_usd = await self._get_ltc_price_usd()

        fees_sat = data.get("fees", 0)
        confirmations = data.get("confirmations", 0)
        confirmed = data.get("confirmed", "")
        block_height = data.get("block_height", 0)
        size = data.get("size", 0)
        inputs = data.get("inputs", [])
        outputs = data.get("outputs", [])

        fees_ltc = fees_sat / 10**8

        input_addrs = {addr for inp in inputs for addr in (inp.get("addresses") or [])}

        sent_sat = sum(
            out.get("value", 0) for out in outputs
            if not any(addr in input_addrs for addr in (out.get("addresses") or []))
        )

        embed = discord.Embed(
            title=f"{Emoji.CRYPTO.value} Litecoin Transaction",
            description=f"[`{tx_hash}`](https://live.blockcypher.com/ltc/tx/{tx_hash})",
            color=0xB0C4DE,
        )
        sent_ltc = sent_sat / 10**8
        sent_str = f"{sent_ltc:.8f} LTC"
        if price_usd:
            sent_str += f" (${sent_ltc * price_usd:,.2f} USD)"
        embed.add_field(name="Amount Sent", value=sent_str, inline=False)

        def _short_addr(addr: str) -> str:
            return f"`{addr[:12]}...{addr[-4:]}`"

        def _group_by_address(items: list[dict], value_key: str) -> dict[str, int]:
            groups: dict[str, int] = {}
            for item in items:
                addrs = item.get("addresses") or []
                val = item.get(value_key) or 0
                addr = addrs[0] if addrs else "unknown"
                groups[addr] = groups.get(addr, 0) + val
            return groups

        def _format_group(groups: dict[str, int]) -> list[str]:
            return [_short_addr(addr) for addr in groups]

        from_groups = _group_by_address(inputs, "output_value")
        to_groups_all = _group_by_address(outputs, "value")

        to_groups = {a: v for a, v in to_groups_all.items() if a not in input_addrs}

        from_lines = _format_group(from_groups)
        to_lines = _format_group(to_groups)

        if from_lines:
            embed.add_field(name="From", value="\n".join(from_lines), inline=False)

        if to_lines:
            embed.add_field(name="To", value="\n".join(to_lines), inline=False)

        fees_str = f"{fees_ltc:.8f} LTC"
        if price_usd:
            fees_str += f" (${fees_ltc * price_usd:,.2f} USD)"
        embed.add_field(name="Fees", value=fees_str, inline=True)
        embed.add_field(name="Confirmations", value=str(confirmations), inline=True)
        embed.add_field(name="Block Height", value=str(block_height), inline=True)
        embed.add_field(name="Size", value=f"{size} bytes", inline=True)

        if confirmed:
            try:
                dt = datetime.fromisoformat(confirmed.replace("Z", "+00:00"))
                embed.add_field(name="Confirmed", value=f"<t:{int(dt.timestamp())}:R>", inline=False)
            except (ValueError, TypeError):
                pass

        await ctx.send(embed=embed)

    @txid.command(name="btc", description="Get information about a Bitcoin transaction.")
    @app_commands.describe(tx_hash="The Bitcoin transaction hash (TXID)")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def btc_txid(self, ctx: commands.Context, tx_hash: str) -> None:
        tx_hash = tx_hash.strip()
        if not tx_hash:
            await self._send_embed(ctx, "Usage: /txid btc <tx_hash>", ephemeral=False)
            return

        url = f"https://api.blockcypher.com/v1/btc/main/txs/{tx_hash}"
        if _BLOCKCYPHER_TOKEN:
            url += f"?token={_BLOCKCYPHER_TOKEN}"

        try:
            async with self.aiohttp.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    data = await response.json()
                elif response.status == 404:
                    await ctx.send(f"Transaction `{tx_hash}` not found on Bitcoin network.")
                    return
                elif response.status == 429:
                    await ctx.send("⚠️ Rate limited by BlockCypher. Please try again later.")
                    return
                else:
                    await ctx.send(f"Error fetching transaction from BlockCypher (Status: {response.status}).")
                    return
        except Exception as exc:
            log_exception(exc)
            await ctx.send("An error occurred while fetching the transaction.")
            return

        price_usd = await self._get_btc_price_usd()

        fees_sat = data.get("fees", 0)
        confirmations = data.get("confirmations", 0)
        confirmed = data.get("confirmed", "")
        block_height = data.get("block_height", 0)
        size = data.get("size", 0)
        inputs = data.get("inputs", [])
        outputs = data.get("outputs", [])

        fees_btc = fees_sat / 10**8

        input_addrs = {addr for inp in inputs for addr in (inp.get("addresses") or [])}

        sent_sat = sum(
            out.get("value", 0) for out in outputs
            if not any(addr in input_addrs for addr in (out.get("addresses") or []))
        )

        embed = discord.Embed(
            title=f"{Emoji.CRYPTO.value} Bitcoin Transaction",
            description=f"[`{tx_hash}`](https://live.blockcypher.com/btc/tx/{tx_hash})",
            color=0xF7931A,
        )

        sent_btc = sent_sat / 10**8
        sent_str = f"{sent_btc:.8f} BTC"
        if price_usd:
            sent_str += f" (${sent_btc * price_usd:,.2f} USD)"
        embed.add_field(name="Amount Sent", value=sent_str, inline=False)

        def _short_addr(addr: str) -> str:
            return f"`{addr[:12]}...{addr[-4:]}`"

        def _group_by_address(items: list[dict], value_key: str) -> dict[str, int]:
            groups: dict[str, int] = {}
            for item in items:
                addrs = item.get("addresses") or []
                val = item.get(value_key) or 0
                addr = addrs[0] if addrs else "unknown"
                groups[addr] = groups.get(addr, 0) + val
            return groups

        from_groups = _group_by_address(inputs, "output_value")
        to_groups_all = _group_by_address(outputs, "value")
        to_groups = {a: v for a, v in to_groups_all.items() if a not in input_addrs}

        from_lines = [_short_addr(addr) for addr in from_groups]
        to_lines = [_short_addr(addr) for addr in to_groups]

        if from_lines:
            embed.add_field(name="From", value="\n".join(from_lines), inline=False)
        if to_lines:
            embed.add_field(name="To", value="\n".join(to_lines), inline=False)

        fees_str = f"{fees_btc:.8f} BTC"
        if price_usd:
            fees_str += f" (${fees_btc * price_usd:,.2f} USD)"
        embed.add_field(name="Fees", value=fees_str, inline=True)
        embed.add_field(name="Confirmations", value=str(confirmations), inline=True)
        embed.add_field(name="Block Height", value=str(block_height), inline=True)
        embed.add_field(name="Size", value=f"{size} bytes", inline=True)
        embed.set_footer(text="NOTE: These values are based on current price of the coin.")
        if confirmed:
            try:
                dt = datetime.fromisoformat(confirmed.replace("Z", "+00:00"))
                embed.add_field(name="Confirmed", value=f"<t:{int(dt.timestamp())}:R>", inline=False)
            except (ValueError, TypeError):
                pass

        await ctx.send(embed=embed)

    @txid.command(name="sol", description="Get information about a Solana transaction.")
    @app_commands.describe(tx_hash="The Solana transaction hash (signature)")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def sol_txid(self, ctx: commands.Context, tx_hash: str) -> None:
        tx_hash = tx_hash.strip()
        if not tx_hash:
            await self._send_embed(ctx, "Usage: /txid sol <tx_hash>", ephemeral=False)
            return

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [tx_hash, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
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
                    await ctx.send(f"Error fetching transaction from Solana RPC (Status: {response.status}).")
                    return
        except Exception as exc:
            log_exception(exc)
            await ctx.send("An error occurred while fetching the transaction.")
            return

        result = data.get("result") if isinstance(data, dict) else None
        if not result:
            await ctx.send(f"Transaction `{tx_hash}` not found on Solana network.")
            return

        meta = result.get("meta") or {}
        tx_message = result.get("transaction", {}).get("message", {})
        account_keys = tx_message.get("accountKeys", [])
        pre_balances = meta.get("preBalances", [])
        post_balances = meta.get("postBalances", [])
        fee_lamports = meta.get("fee", 0)
        block_time = result.get("blockTime")
        slot = result.get("slot", 0)
        err = meta.get("err")

        if err:
            await ctx.send("❌ This transaction failed.")
            return

        sender_addr = account_keys[0] if account_keys else "unknown"

        receiver_addr = None
        received_lamports = 0
        for i, addr in enumerate(account_keys):
            if i == 0:
                continue
            if i < len(pre_balances) and i < len(post_balances):
                diff = post_balances[i] - pre_balances[i]
                if diff > 0:
                    receiver_addr = addr
                    received_lamports = diff
                    break

        price_usd = await self._get_price_usd("solana", _SOL_PRICE_CACHE_KEY)

        sent_lamports = received_lamports
        sent_sol = sent_lamports / 10**9
        fees_sol = fee_lamports / 10**9

        embed = discord.Embed(
            title=f"{Emoji.CRYPTO.value} Solana Transaction",
            description=f"[`{tx_hash}`](https://solscan.io/tx/{tx_hash})",
            color=0x00FFA3,
        )

        sent_str = f"{sent_sol:.9f} SOL"
        if price_usd:
            sent_str += f" (${sent_sol * price_usd:,.2f} USD)"
        embed.add_field(name="Amount Sent", value=sent_str, inline=False)

        if sender_addr:
            embed.add_field(name="From", value=f"`{sender_addr[:12]}...{sender_addr[-4:]}`", inline=False)
        if receiver_addr:
            embed.add_field(name="To", value=f"`{receiver_addr[:12]}...{receiver_addr[-4:]}`", inline=False)

        fees_str = f"{fees_sol:.9f} SOL"
        if price_usd:
            fees_str += f" (${fees_sol * price_usd:,.2f} USD)"
        embed.add_field(name="Fees", value=fees_str, inline=True)
        embed.add_field(name="Slot", value=str(slot), inline=True)
        embed.set_footer(text="NOTE: These values are based on current price of the coin.")
        if block_time:
            embed.add_field(name="Confirmed", value=f"<t:{block_time}:R>", inline=False)

        await ctx.send(embed=embed)

    @txid.command(name="eth", description="Get information about an Ethereum transaction.")
    @app_commands.describe(tx_hash="The Ethereum transaction hash (TXID)")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def eth_txid(self, ctx: commands.Context, tx_hash: str) -> None:
        tx_hash = tx_hash.strip()
        if not tx_hash:
            await self._send_embed(ctx, "Usage: /txid eth <tx_hash>", ephemeral=False)
            return

        url = f"https://api.blockcypher.com/v1/eth/main/txs/{tx_hash}"
        if _BLOCKCYPHER_TOKEN:
            url += f"?token={_BLOCKCYPHER_TOKEN}"

        try:
            async with self.aiohttp.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    data = await response.json()
                elif response.status == 404:
                    await ctx.send(f"Transaction `{tx_hash}` not found on Ethereum network.")
                    return
                elif response.status == 429:
                    await ctx.send("⚠️ Rate limited by BlockCypher. Please try again later.")
                    return
                else:
                    await ctx.send(f"Error fetching transaction from BlockCypher (Status: {response.status}).")
                    return
        except Exception as exc:
            log_exception(exc)
            await ctx.send("An error occurred while fetching the transaction.")
            return

        price_usd = await self._get_price_usd("ethereum", _ETH_PRICE_CACHE_KEY)

        fees_wei = data.get("fees", 0)
        confirmations = data.get("confirmations", 0)
        confirmed = data.get("confirmed", "")
        block_height = data.get("block_height", 0)
        inputs = data.get("inputs", [])
        outputs = data.get("outputs", [])

        fees_eth = fees_wei / 10**18

        input_addrs = {addr for inp in inputs for addr in (inp.get("addresses") or [])}

        sent_wei = sum(
            out.get("value", 0) for out in outputs
            if not any(addr in input_addrs for addr in (out.get("addresses") or []))
        )

        embed = discord.Embed(
            title=f"{Emoji.CRYPTO.value} Ethereum Transaction",
            description=f"[`{tx_hash}`](https://live.blockcypher.com/eth/tx/{tx_hash[2:]})",
            color=0x627EEA,
        )

        sent_eth = sent_wei / 10**18
        sent_str = f"{sent_eth:.8f} ETH"
        if price_usd:
            sent_str += f" (${sent_eth * price_usd:,.2f} USD)"
        embed.add_field(name="Amount Sent", value=sent_str, inline=False)

        def _short_addr(addr: str) -> str:
            return f"`{addr[:12]}...{addr[-4:]}`"

        def _group_by_address(items: list[dict], value_key: str) -> dict[str, int]:
            groups: dict[str, int] = {}
            for item in items:
                addrs = item.get("addresses") or []
                val = item.get(value_key) or 0
                addr = addrs[0] if addrs else "unknown"
                groups[addr] = groups.get(addr, 0) + val
            return groups

        from_groups = _group_by_address(inputs, "output_value")
        to_groups_all = _group_by_address(outputs, "value")
        to_groups = {a: v for a, v in to_groups_all.items() if a not in input_addrs}

        from_lines = [_short_addr(addr) for addr in from_groups]
        to_lines = [_short_addr(addr) for addr in to_groups]

        if from_lines:
            embed.add_field(name="From", value="\n".join(from_lines), inline=False)
        if to_lines:
            embed.add_field(name="To", value="\n".join(to_lines), inline=False)

        fees_str = f"{fees_eth:.8f} ETH"
        if price_usd:
            fees_str += f" (${fees_eth * price_usd:,.2f} USD)"
        embed.add_field(name="Fees", value=fees_str, inline=True)
        embed.add_field(name="Confirmations", value=str(confirmations), inline=True)
        embed.add_field(name="Block Height", value=str(block_height), inline=True)
        embed.set_footer(text="NOTE: These values are based on current price of the coin.")
        if confirmed:
            try:
                dt = datetime.fromisoformat(confirmed.replace("Z", "+00:00"))
                embed.add_field(name="Confirmed", value=f"<t:{int(dt.timestamp())}:R>", inline=False)
            except (ValueError, TypeError):
                pass

        await ctx.send(embed=embed)

    @txid.command(name="usdt-bep20", description="Get information about a USDT BEP-20 transaction on BSC.")
    @app_commands.describe(tx_hash="The BSC transaction hash")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def usdt_bep20_txid(self, ctx: commands.Context, tx_hash: str) -> None:
        tx_hash = tx_hash.strip()
        if not tx_hash:
            await self._send_embed(ctx, "Usage: /txid usdt-bep20 <tx_hash>", ephemeral=False)
            return

        url = f"https://api.bscscan.com/api?module=account&action=tokentx&txhash={tx_hash}&sort=asc"
        if _BSCSCAN_API_KEY:
            url += f"&apikey={_BSCSCAN_API_KEY}"

        data = await self._fetch_json(url)
        if not data or data.get("status") != "1":
            await ctx.send(f"Transaction `{tx_hash}` not found or no token transfers on BSC.")
            return

        transfers = data.get("result", [])
        if not isinstance(transfers, list):
            await ctx.send(f"Transaction `{tx_hash}` not found or no token transfers on BSC.")
            return

        usdt_transfers = [t for t in transfers if t.get("tokenSymbol", "").upper() == "USDT"]
        if not usdt_transfers:
            await ctx.send(f"No USDT transfer found in transaction `{tx_hash}`.")
            return

        tx_data = usdt_transfers[0]

        price_usd = await self._get_price_usd("tether", "usdt:usd_price")

        from_addr = tx_data.get("from", "")
        to_addr = tx_data.get("to", "")
        value_dec = tx_data.get("value", "0")
        token_symbol = tx_data.get("tokenSymbol", "USDT")
        token_decimal = int(tx_data.get("tokenDecimal", 18))
        try:
            value_float = float(value_dec) / (10**token_decimal)
        except (ValueError, TypeError):
            value_float = 0.0

        gas_used = int(tx_data.get("gasUsed", 0))
        gas_price = int(tx_data.get("gasPrice", 0))
        gas_fee_bnb = (gas_used * gas_price) / 10**18
        confirmations = tx_data.get("confirmations", "0")
        block_number = tx_data.get("blockNumber", "0")
        time_stamp = tx_data.get("timeStamp", "0")

        embed = discord.Embed(
            title=f"{Emoji.CRYPTO.value} USDT (BEP-20) Transaction",
            description=f"[`{tx_hash[:16]}...`](https://bscscan.com/tx/{tx_hash})",
            color=0xF0B90B,
        )

        sent_str = f"{value_float:,.2f} {token_symbol}"
        if price_usd:
            sent_str += f" (${value_float * price_usd:,.2f} USD)"
        embed.add_field(name="Amount Sent", value=sent_str, inline=False)

        embed.add_field(name="From", value=f"`{from_addr[:12]}...{from_addr[-4:]}`", inline=False)
        embed.add_field(name="To", value=f"`{to_addr[:12]}...{to_addr[-4:]}`", inline=False)

        embed.add_field(name="Gas Fee", value=f"{gas_fee_bnb:.8f} BNB", inline=True)
        embed.add_field(name="Confirmations", value=str(confirmations), inline=True)
        embed.add_field(name="Block", value=str(block_number), inline=True)
        embed.set_footer(text="NOTE: These values are based on current price of the coin.")
        try:
            ts = int(time_stamp)
            embed.add_field(name="Confirmed", value=f"<t:{ts}:R>", inline=False)
        except (ValueError, TypeError):
            pass

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
            emoji = Emoji.UPSHIFT.value if change_24h >= 0 else Emoji.DOWNSHIFT.value
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


    # addy helpers

    def _addy_cache_key(self, user_id: int) -> str:
        return f"addy:{user_id}"

    def _invalidate_addy_cache(self, user_id: int) -> None:
        cache.delete(self._addy_cache_key(user_id))

    async def _get_user_addresses(self, user_id: int) -> dict[str, str]:
        key = self._addy_cache_key(user_id)

        async def _fetch() -> dict[str, str]:
            cursor = await self.addy_conn.execute(
                "SELECT coin, address FROM addresses WHERE user_id = ?",
                (user_id,),
            )
            rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}

        return await cache.get_or_set_async(key, _fetch, ttl=300)  # type: ignore[return-value]

    async def _set_user_address(self, user_id: int, coin: str, address: str) -> None:
        await self.addy_conn.execute(
            "INSERT OR REPLACE INTO addresses (user_id, coin, address) VALUES (?, ?, ?)",
            (user_id, coin, address),
        )
        await self.addy_conn.commit()
        self._invalidate_addy_cache(user_id)

    # /addy group

    @commands.hybrid_group(
        name="addy",
        description="Manage your saved crypto addresses",
        invoke_without_command=True,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def addy(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        addresses = await self._get_user_addresses(ctx.author.id)
        if not addresses:
            await self._send_embed(
                ctx,
                "You have no saved addresses.\n"
                "Use `/addy set <network> <addy>` to add one.",
                ephemeral=False,
            )
            return
        lines = [f"**{coin.upper()}:** `{addr}`" for coin, addr in sorted(addresses.items())]
        embed = discord.Embed(
            title=f"{Emoji.CRYPTO.value} Your saved addresses",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text="Use /addy set <network> <addy> to update",
        )
        await ctx.send(embed=embed)

    @addy.command(name="set", description="Save one of your crypto addresses")
    @app_commands.describe(network="The network to save", addy="Your crypto address")
    @app_commands.choices(network=CRYPTO_NETWORK_CHOICES)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def addy_set(self, ctx: commands.Context, network: str, *, addy: str) -> None:
        network = network.strip().lower()
        network_data = CRYPTO_NETWORK_BY_VALUE.get(network)
        if not network_data:
            await self._send_embed(ctx, "Please choose a supported network.", ephemeral=True)
            return

        addy = addy.strip()
        if not addy:
            await self._send_embed(ctx, f"Please provide a {network_data.name} address.", ephemeral=False)
            return

        await self._set_user_address(ctx.author.id, network, addy)
        await self._send_embed(
            ctx,
            f"Your {network_data.name} address has been saved: `{addy}`",
            ephemeral=True,
        )

    # /addy get lookup

    async def _addy_lookup(self, ctx: commands.Context, network: str, display: str) -> None:
        addresses = await self._get_user_addresses(ctx.author.id)
        addr = addresses.get(network)
        if not addr:
            await self._send_embed(
                ctx,
                f"You have no saved {display} address.\nUse `/addy set {network} <addy>` to add one.",
                ephemeral=True,
            )
            return
        await self._send_embed(
            ctx,
            f"{addr}",
            ephemeral=False,
        )

    @addy.command(name="get", description="Get one of your saved crypto addresses")
    @app_commands.describe(network="The network to show")
    @app_commands.choices(network=CRYPTO_NETWORK_CHOICES)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def addy_get(self, ctx: commands.Context, network: str) -> None:
        network = network.strip().lower()
        network_data = CRYPTO_NETWORK_BY_VALUE.get(network)
        if not network_data:
            await self._send_embed(ctx, "Please choose a supported network.", ephemeral=True)
            return
        await self._addy_lookup(ctx, network, network_data.name)


async def setup(bot: Amenity) -> None:
    await bot.add_cog(Crypto(bot))
