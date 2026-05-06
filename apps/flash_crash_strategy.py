"""
Polymarket Arbitrage Bot - Flash Crash Trading Strategy

A volatility trading strategy designed for Polymarket's 15-minute Up/Down
markets. This strategy identifies sudden probability drops 
and executes trades to capitalize on market inefficiencies.

Strategy Overview:
    The Flash Crash strategy monitors 15-minute prediction markets in
    real-time and detects when either the "Up" or "Down" probability
    experiences a significant drop within a short time window. When such
    an event is detected, the strategy automatically purchases the crashed
    side, expecting a mean reversion.

Strategy Logic:
1. Auto-discover current 15-minute market for selected coin (BTC, ETH, SOL, XRP)
2. Monitor orderbook prices in real-time via WebSocket
3. Track price history and detect probability drops
4. When drop threshold is exceeded within lookback window:
   - Execute market buy order on the crashed side
5. Manage position with exit conditions:
   - Take profit: Configurable dynamic percentage (default: +50%)
   - Stop loss: Configurable dynamic percentage (default: -50%)

Usage:
    from apps.flash_crash_strategy import FlashCrashStrategy, FlashCrashConfig
    from src import TradingBot

    bot = TradingBot(...)
    config = FlashCrashConfig(
        coin="BTC",
        drop_threshold=0.30,  # 30% probability drop
        trade_size=10.0,      # 10 tokens per trade
        lookback_seconds=10,  # 10-second detection window
        take_profit=0.50,     # 50% take profit
        stop_loss=0.50        # 50% stop loss
    )
    strategy = FlashCrashStrategy(bot, config)
    await strategy.run()

Risk Warning:
    This strategy involves significant risk. Flash crashes can continue
    beyond expected reversion points. Always use appropriate position sizing
    and risk management. Test thoroughly before using with real funds.
"""

import asyncio
from dataclasses import dataclass
from typing import Dict

from lib.terminal_utils import Colors, format_countdown
from apps.base_strategy import BaseStrategy, StrategyConfig
from src.bot import TradingBot
from src.websocket_client import OrderbookSnapshot


@dataclass
class FlashCrashConfig(StrategyConfig):
    """Flash crash strategy configuration."""
    drop_threshold: float = 0.30  # Absolute probability drop

class FlashCrashStrategy(BaseStrategy):
    """
    Flash Crash Trading Strategy.

    Monitors markets for sudden price drops, and trades
    the volatility with defined take-profit and stop-loss levels.
    """

    def __init__(self, bot: TradingBot, config: FlashCrashConfig):
        """Initialize flash crash strategy."""
        super().__init__(bot, config)
        self.flash_config = config

        # Update price tracker with our thresholds
        self.prices.drop_threshold = config.drop_threshold

    async def on_book_update(self, snapshot: OrderbookSnapshot) -> None:
        """Handle orderbook update - check for flash crashes."""
        # Note: Actual price recording for flash crash detection is done in base class
        pass

    async def on_tick(self, prices: Dict[str, float]) -> None:
        """Check for flash crash on each tick."""
        market = self.current_market
        if market and market.is_ending_soon(60):
            return

        if not self.positions.can_open_position:
            return

        drop_event = self.prices.detect_flash_drop()

        if drop_event:
            self.log(
                f"FLASH CRASH: {drop_event.side.upper()} "
                f"drop {drop_event.drop:.2f} ({drop_event.old_price:.2f} -> {drop_event.new_price:.2f})",
                "trade"
            )
            self.prices.clear()  # Clear ALL history to prevent concurrent triggers on opposite side
            
            # Use the already calculated mid price for buying
            mid_price = prices.get(drop_event.side, 0)
            
            if mid_price > 0:
                await self.execute_open(drop_event.side, mid_price)

    def get_status_lines(self, prices: Dict[str, float]) -> list[str]:
        """Get TUI status lines (Compact version)."""
        lines = []

        ws_status = f"{Colors.GREEN}WS{Colors.RESET}" if self.is_connected else f"{Colors.RED}REST{Colors.RESET}"
        countdown = self._get_countdown_str()
        stats = self.positions.get_stats()
        total_pnl = self.positions.get_total_pnl(prices)

        # Line 1: Header & Main Stats
        pnl_color = Colors.GREEN if total_pnl >= 0 else Colors.RED
        header = (
            f"{Colors.BOLD}{Colors.CYAN}[{self.config.coin:4}]{Colors.RESET} {ws_status} | "
            f"Ends: {countdown} | "
            f"PnL: {pnl_color}${total_pnl:+.2f}{Colors.RESET} | "
            f"W/L: {stats['winning_trades']}/{stats['losing_trades']} ({stats['win_rate']:.0f}%)"
        )
        lines.append(header)

        # Line 2: Parameters
        tp_fmt = f"+{self.config.take_profit*100:.0f}%"
        sl_fmt = f"-{self.config.stop_loss*100:.0f}%"
        params = (
            f"  ⚙️ Size:{self.config.size:.0f} | Drop:{self.flash_config.drop_threshold:.2f} "
            f"| TP/SL:{tp_fmt}/{sl_fmt} | Spread<={self.config.max_spread:.3f}"
        )
        lines.append(params)

        # Line 3: Orderbook Summary & Spread
        up_ob = self.market.get_orderbook("up") if self.market else None
        down_ob = self.market.get_orderbook("down") if self.market else None
        
        up_mid = up_ob.mid_price if up_ob else prices.get("up", 0)
        down_mid = down_ob.mid_price if down_ob else prices.get("down", 0)
        
        # If orderbook is available but mid_price calculation gives 0/default, try using prices directly
        if up_mid == 0 or up_mid == 0.5:
            up_mid = prices.get("up", up_mid)
        if down_mid == 0 or down_mid == 0.5:
            down_mid = prices.get("down", down_mid)
            
        up_spread = self.market.get_spread("up") if self.market else 0.0
        down_spread = self.market.get_spread("down") if self.market else 0.0
        
        up_info = f"{Colors.GREEN}UP{Colors.RESET}: {up_mid:.4f} (Spr: {up_spread:.4f})"
        down_info = f"{Colors.RED}DN{Colors.RESET}: {down_mid:.4f} (Spr: {down_spread:.4f})"
        
        up_history_max = self.prices.get_history_count("up")
        down_history_max = self.prices.get_history_count("down")
        history = f"Hist: U{up_history_max}/D{down_history_max}"
        
        lines.append(f"  📊 {up_info} | {down_info} | {history}")

        # Line 4: Orderbook depth (Top 2 levels only)
        if up_ob and down_ob and len(up_ob.bids) > 0 and len(down_ob.bids) > 0:
            levels = []
            for i in range(min(2, len(up_ob.bids), len(down_ob.bids))):
                u_b, u_a = up_ob.bids[i], up_ob.asks[i] if i < len(up_ob.asks) else None
                d_b, d_a = down_ob.bids[i], down_ob.asks[i] if i < len(down_ob.asks) else None
                
                u_str = f"{Colors.GREEN}{u_b.price:.3f}({u_b.size:.0f})v{u_a.price:.3f}({u_a.size:.0f}){Colors.RESET}" if u_a else f"{u_b.price:.3f}({u_b.size:.0f})v---"
                d_str = f"{Colors.RED}{d_b.price:.3f}({d_b.size:.0f})v{d_a.price:.3f}({d_a.size:.0f}){Colors.RESET}" if d_a else f"{d_b.price:.3f}({d_b.size:.0f})v---"
                levels.append(f"L{i+1}[{u_str} | {d_str}]")
            
            lines.append(f"  🕮 {'  '.join(levels)}")

        # Line 5: Active Positions / Orders
        active_items = []
        all_positions = self.positions.get_all_positions()
        
        for pos in all_positions:
            current = prices.get(pos.side, 0)
            pnl = pos.get_pnl(current)
            color = Colors.GREEN if pnl >= 0 else Colors.RED
            status_indicator = ""
            if pos.status == "pending_open":
                status_indicator = "(PO)"
            elif pos.status == "pending_close":
                status_indicator = "(PC)"
            active_items.append(f"POS:{pos.side.upper()}{status_indicator} {pos.size:.0f}@{pos.entry_price:.3f} {color}${pnl:+.2f}{Colors.RESET} [TP:{pos.take_profit_price:.3f} SL:{pos.stop_loss_price:.3f}]")
            
        market_orders = self.current_market_open_orders
        for order in market_orders[:2]:  # Max 2 orders
            token_side = "UP" if order.asset_id == self.token_ids.get("up") else "DN"
            color = Colors.GREEN if order.side == "BUY" else Colors.RED
            active_items.append(f"ORD:{color}{order.side[0]}{Colors.RESET}:{token_side} {order.size_matched:.0f}/{order.original_size:.0f}@{order.price:.3f}")

        if active_items:
            lines.append(f"  🎯 {' | '.join(active_items)}")
            
        # Add a subtle separator for multi-coin view
        lines.append(f"{Colors.DIM}{'-'*60}{Colors.RESET}")
                
        return lines


    def _get_countdown_str(self) -> str:
        """Get formatted countdown string."""
        market = self.current_market
        if not market:
            return "--:--"

        mins, secs = market.get_countdown()
        return format_countdown(mins, secs)

    def on_market_change(self, old_slug: str, new_slug: str) -> None:
        """Handle market change - clear price history."""
        self.prices.clear()

    def _print_summary(self) -> None:
        self._status_mode = False
