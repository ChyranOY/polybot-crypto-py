#!/usr/bin/env python3
"""
Polymarket Arbitrage Bot - Flash Crash Strategy Runner

Command-line entry point for running the Flash Crash trading strategy
on Polymarket markets. This script is fully driven by a JSON configuration
file, allowing you to run multiple coins concurrently with independent
parameters in a single process.

Usage:
    # Run strategy using a configuration file
    python apps/flash_crash_runner.py --config-file config.json

    # Run in dry-run mode (no real trades)
    python apps/flash_crash_runner.py --config-file config.json --dry-run

    # Run with debug logging
    python apps/flash_crash_runner.py --config-file config.json --debug

Arguments:
    --config-file   Path to a JSON configuration file [Required]
    --dry-run       Enable dry run mode (no real trades) globally
    --debug         Enable debug logging globally

Example config.json:
    {
      "BTC": {
        "interval": "5m",
        "size": 11,
        "drop": 0.40,
        "lookback": 5,
        "take_profit": 0.80,
        "stop_loss": 0.80,
        "slippage": 0.045,
        "max_spread": 0.021
      },
      "ETH": {
        "interval": "15m",
        "size": 100,
        "drop": 0.30
      }
    }

Prerequisites:
    - Python 3.8 or higher
    - All dependencies installed (see requirements.txt)
    - A .env file with PRIVATE_KEY and PROXY_WALLET

Risk Warning:
    This strategy involves financial risk. Test thoroughly with small
    amounts before committing larger funds. Past performance does not
    guarantee future results.
"""

import os
import sys
import asyncio
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone

# Suppress noisy logs
logging.getLogger("src.websocket_client").setLevel(logging.WARNING)
logging.getLogger("src.bot").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Auto-load .env file
from dotenv import load_dotenv
load_dotenv()

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.terminal_utils import Colors
from src.bot import TradingBot
from src.config import Config
from apps.flash_crash_strategy import FlashCrashStrategy, FlashCrashConfig


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Flash Crash Strategy for Polymarket markets"
    )
    parser.add_argument(
        "--config-file",
        type=str,
        required=True,
        help="Path to a JSON configuration file for per-coin settings"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Enable dry run mode (no real trades) globally"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging globally"
    )

    # Parse arguments
    args = parser.parse_args()

    # Enable debug logging if requested
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger("src.websocket_client").setLevel(logging.DEBUG)
        logging.getLogger("httpx").setLevel(logging.DEBUG)

    log_dir = Path(__file__).parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Check environment
    private_key = os.environ.get("PRIVATE_KEY")
    safe_address = os.environ.get("PROXY_WALLET")

    if not private_key or not safe_address:
        print(f"{Colors.RED}Error: PRIVATE_KEY and PROXY_WALLET must be set{Colors.RESET}")
        print("Set them in .env file or export as environment variables")
        sys.exit(1)

    # Create bot
    config = Config.from_env()
    bot = TradingBot(config=config, private_key=private_key)

    if not bot.is_initialized():
        print(f"{Colors.RED}Error: Failed to initialize bot{Colors.RESET}")
        sys.exit(1)

    import json
    import traceback
    
    # Load per-coin overrides
    overrides = {}
    
    # Load from config file
    try:
        with open(args.config_file, 'r', encoding='utf-8') as f:
            file_config = json.load(f)
            for k, v in file_config.items():
                overrides[k.upper()] = v
    except Exception as e:
        print(f"{Colors.RED}Error loading config file '{args.config_file}': {e}{Colors.RESET}")
        sys.exit(1)
        
    coins = list(overrides.keys())
    if not coins:
        print(f"{Colors.RED}Error: No valid coins found in config file{Colors.RESET}")
        sys.exit(1)

    # Generate log filename with coin and interval
    timestamp_str = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')

    strategies = []
    
    print(f"\n{Colors.BOLD}{'='*60}{Colors.RESET}")
    print(f"{Colors.BOLD}  Flash Crash Strategy - Markets: {', '.join(coins)}{Colors.RESET}")
    print(f"{Colors.BOLD}{'='*60}{Colors.RESET}\n")

    if overrides:
        print(f"\n{Colors.BOLD}Loaded Configuration:{Colors.RESET}")
        for c, cfg in overrides.items():
            if c in coins:
                print(f"  {Colors.CYAN}{c}{Colors.RESET}:")
                for k, v in cfg.items():
                    print(f"    {k}: {v}")
    print()

    for coin in coins:
        coin_overrides = overrides.get(coin, {})
        
        # Merge file config with base defaults
        strategy_config = FlashCrashConfig(
            coin=coin,
            market_interval=coin_overrides.get("interval", "15m"),
            size=coin_overrides.get("size", 100.0),
            drop_threshold=coin_overrides.get("drop", 0.30),
            price_lookback_seconds=coin_overrides.get("lookback", 15),
            take_profit=coin_overrides.get("take_profit", 0.20),
            stop_loss=coin_overrides.get("stop_loss", 0.10),
            max_size=coin_overrides.get("max_size", 500.0),
            min_price=coin_overrides.get("min_price", 0.11),
            max_price=coin_overrides.get("max_price", 0.89),
            max_spread=coin_overrides.get("max_spread", 0.025),
            dry_run=coin_overrides.get("dry_run", args.dry_run),
            slippage=coin_overrides.get("slippage", 0.01)
        )
        
        strategy = FlashCrashStrategy(bot=bot, config=strategy_config)
        strategies.append(strategy)

    # Multi-strategy renderer
    async def multi_render_loop(strategies):
        while True:
            lines = ["\033[H\033[J"]
            for strategy in strategies:
                if not strategy.running:
                    continue
                # Get current effective prices for UI rendering
                prices = strategy._get_exit_prices()
                
                # Fallback to mid prices if exit prices are missing/0
                mid_prices = strategy._get_mid_prices()
                for side in ["up", "down"]:
                    if side not in prices or prices[side] == 0:
                        prices[side] = mid_prices.get(side, 0)
                        
                lines.extend(strategy.get_status_lines(prices))
                lines.append("\n")
                
            if len(lines) > 1:
                print("\n".join(lines), flush=True)
                import sys
                sys.stdout.flush()
            await asyncio.sleep(0.1)

    async def run_all(strategies):
        # If running multiple, disable individual render
        if len(strategies) > 1:
            for strategy in strategies:
                strategy.render_status = lambda prices: None
            asyncio.create_task(multi_render_loop(strategies))
            
        tasks = [strategy.run() for strategy in strategies]
        await asyncio.gather(*tasks)

    try:
        asyncio.run(run_all(strategies))
    except KeyboardInterrupt:
        print("\nInterrupted")
    except Exception as e:
        print(f"\n{Colors.RED}Error: {e}{Colors.RESET}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
