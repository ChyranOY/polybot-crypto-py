import os
import sys
import asyncio
import argparse
from pathlib import Path
from typing import List

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import Config
from src.bot import TradingBot
from src.gamma_client import GammaClient


def _env_flags() -> List[str]:
    required = ["PRIVATE_KEY", "PROXY_WALLET"]
    missing = [k for k in required if not os.environ.get(k)]
    builder_vars = [
        "POLY_RELAYER_API_KEY",
        "POLY_RELAYER_API_KEY_ADDRESS",
    ]
    builder_values = {k: os.environ.get(k, "") for k in builder_vars}
    builder_present = any(v for v in builder_values.values())
    builder_complete = all(v for v in builder_values.values())
    quoted = [k for k in required if (os.environ.get(k, "").startswith(("'", '"')) or os.environ.get(k, "").endswith(("'", '"')))]

    lines = []
    if missing:
        lines.append(f"Missing required env: {', '.join(missing)}")
    else:
        lines.append("Required env present")
    if builder_present and not builder_complete:
        lines.append("Relayer env incomplete (API key/address)")
    if not builder_present:
        lines.append("Relayer env not set")
    if builder_complete:
        lines.append("Relayer env complete")
    if quoted:
        lines.append(f"Potentially quoted values: {', '.join(quoted)}")
    return lines


async def _check_orders(bot: TradingBot) -> None:
    try:
        orders = await bot.get_open_orders()
        print(f"Open orders check OK (count={len(orders)})")
    except Exception as e:
        message = str(e)
        print(f"Open orders check FAILED: {message}")
        if "401" in message:
            print("Auth failed (401). Check private key/proxy wallet match and builder credentials.")


def _check_gamma(coin: str, interval: str) -> None:
    client = GammaClient()
    market = client.get_market_info(coin, interval=interval)
    if not market:
        print("Gamma market check FAILED")
        return
    slug = market.get("slug", "")
    accepting = market.get("accepting_orders", False)
    print(f"Gamma market OK (slug={slug}, accepting_orders={accepting})")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket config self-check (no orders placed)")
    parser.add_argument("--coin", type=str, default="BTC")
    parser.add_argument("--interval", type=str, default="5m", choices=["5m", "15m", "1h"])
    args = parser.parse_args()

    for line in _env_flags():
        print(line)

    private_key = os.environ.get("PRIVATE_KEY", "")
    config = Config.from_env()
    errors = config.validate()
    if errors:
        print(f"Config validation errors: {errors}")
    else:
        print("Config validation OK")

    if not private_key:
        print("Bot init skipped (missing private key)")
        return

    bot = TradingBot(config=config, private_key=private_key)
    if not bot.is_initialized():
        print("Bot init FAILED")
        return

    print("Bot init OK")
    await _check_orders(bot)
    _check_gamma(args.coin.upper(), args.interval)


if __name__ == "__main__":
    asyncio.run(main())
