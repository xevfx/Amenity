from dataclasses import dataclass

from discord import app_commands


@dataclass(frozen=True)
class CryptoNetwork:
    value: str
    name: str
    qr_scheme: str | None = None


CRYPTO_NETWORKS: tuple[CryptoNetwork, ...] = (
    CryptoNetwork("btc", "Bitcoin", "bitcoin"),
    CryptoNetwork("eth", "Ethereum", "ethereum"),
    CryptoNetwork("ltc", "Litecoin", "litecoin"),
    CryptoNetwork("sol", "Solana", "solana"),
    CryptoNetwork("doge", "Dogecoin", "dogecoin"),
    CryptoNetwork("bch", "Bitcoin Cash", "bitcoincash"),
    CryptoNetwork("xrp", "XRP", "ripple"),
    CryptoNetwork("ada", "Cardano", "cardano"),
    CryptoNetwork("dot", "Polkadot", "polkadot"),
    CryptoNetwork("trx", "TRON", "tron"),
    CryptoNetwork("bnb", "BNB Smart Chain", "bnb"),
    CryptoNetwork("matic", "Polygon", "polygon"),
    CryptoNetwork("avax", "Avalanche", "avalanche"),
    CryptoNetwork("atom", "Cosmos", "cosmos"),
    CryptoNetwork("xmr", "Monero", "monero"),
    CryptoNetwork("zec", "Zcash", "zcash"),
    CryptoNetwork("dash", "Dash", "dash"),
    CryptoNetwork("usdt-erc20", "USDT (ERC-20)", "ethereum"),
    CryptoNetwork("usdt-trc20", "USDT (TRC-20)", "tron"),
    CryptoNetwork("usdc-erc20", "USDC (ERC-20)", "ethereum"),
)

CRYPTO_NETWORK_BY_VALUE = {network.value: network for network in CRYPTO_NETWORKS}
CRYPTO_NETWORK_CHOICES = [
    app_commands.Choice(name=network.name, value=network.value)
    for network in CRYPTO_NETWORKS
]


def crypto_qr_payload(network: CryptoNetwork, address: str) -> str:
    if network.qr_scheme:
        return f"{network.qr_scheme}:{address}"
    return address
