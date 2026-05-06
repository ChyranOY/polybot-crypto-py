"""
Polymarket Arbitrage Bot - Strategy Base Class

Abstract base class that provides the foundation for implementing custom
trading strategies. This class handles common functionality including
market management, price tracking, position management, and lifecycle
control.

Features:
    - Common lifecycle methods (start, stop, run, cleanup)
    - Integration with lib components:
      * MarketManager: Market discovery and WebSocket management
      * PriceTracker: Real-time price tracking and pattern detection
      * PositionManager: Position tracking with take-profit/stop-loss
    - Built-in logging and status display utilities
    - Event-driven architecture with async callbacks

Usage:
    from apps.base_strategy import BaseStrategy, StrategyConfig
    from src import TradingBot

    class MyStrategy(BaseStrategy):
        async def on_book_update(self, snapshot):
            \"\"\"Handle orderbook updates.\"\"\"
            # Your trading logic here
            mid_price = snapshot.mid_price
            if mid_price < 0.3:
                await self.bot.place_order(...)

        async def on_tick(self, prices):
            \"\"\"Called each strategy tick (polling interval).\"\"\"
            # Periodic logic here
            pass

    # Initialize and run
    bot = TradingBot(...)
    config = StrategyConfig(...)
    strategy = MyStrategy(bot, config)
    await strategy.run()

Note:
    Subclasses must implement on_book_update() or on_tick() methods
    to define the strategy's trading logic. The base class handles
    all infrastructure concerns automatically.
"""

import asyncio
import time
import logging
import os
from pathlib import Path
from datetime import datetime, timezone
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, List

from lib.terminal_utils import LogBuffer, log
from lib.market_manager import MarketManager, MarketInfo
from lib.price_tracker import PriceTracker
from lib.position_manager import PositionManager, Position
from src.bot import TradingBot
from src.websocket_client import OrderbookSnapshot, UserWebSocket, TradeEvent, OrderEvent


@dataclass
class StrategyConfig:
    """Base strategy configuration."""

    coin: str = "BTC"
    size: float = 5.0  # Token size per trade
    max_positions: int = 1
    take_profit: float = 0.50  # 50% TP
    stop_loss: float = 0.50    # 50% SL

    # Market settings
    market_interval: str = "5m"
    market_check_interval: float = 1.0
    auto_switch_market: bool = True

    # Price tracking
    price_lookback_seconds: int = 10

    # Size limits
    max_size: float = 0.0
    min_price: float = 0.01
    max_price: float = 0.99
    max_spread: float = 0.025  # Maximum allowed spread to open a position

    # Display settings
    update_interval: float = 0.1
    order_refresh_interval: float = 2.0  # Seconds between order refreshes
    dry_run: bool = False
    slippage: float = 0.025  # Slippage/buffer for market orders


class BaseStrategy(ABC):
    """
    Base class for trading strategies.

    Provides common infrastructure:
    - MarketManager for WebSocket and market discovery
    - PriceTracker for price history
    - PositionManager for positions and TP/SL
    - Logging and status display
    """

    def __init__(self, bot: TradingBot, config: StrategyConfig):
        """
        Initialize base strategy.

        Args:
            bot: TradingBot instance for order execution
            config: Strategy configuration
        """
        self.bot = bot
        self.config = config

        # Core components
        self.market = MarketManager(
            coin=config.coin,
            market_interval=config.market_interval,
            market_check_interval=config.market_check_interval,
            auto_switch_market=config.auto_switch_market,
        )

        self.prices = PriceTracker(
            lookback_seconds=config.price_lookback_seconds,
        )

        self.positions = PositionManager(
            take_profit=config.take_profit,
            stop_loss=config.stop_loss,
            max_positions=config.max_positions,
        )

        # State
        self.running = False
        self._status_mode = False

        # Setup isolated file logger for this coin strategy
        self._setup_logger()

        # Logging buffer for UI
        self._log_buffer = LogBuffer(max_size=5)

        self._last_exit_prices: Dict[str, float] = {}

        # Open Orders tracked locally via WS
        self._active_orders: Dict[str, OrderEvent] = {}

        # Background task tracking to ensure clean shutdown
        self._background_tasks: set[asyncio.Task] = set()

        # User WebSocket
        self.user_ws: Optional[UserWebSocket] = None
        self._user_ws_task: Optional[asyncio.Task] = None

    def _setup_logger(self) -> None:
        """Setup isolated file logger for this coin strategy."""
        log_dir = Path(__file__).parent.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp_str = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        log_filename = f"flash_crash_{self.config.coin.lower()}_{self.config.market_interval}_{timestamp_str}.log"
        log_path = log_dir / log_filename
        
        self._logger = logging.getLogger(f"Strategy_{self.config.coin}_{id(self)}")
        self._logger.propagate = False  # Don't pass to root logger
        
        # Set level based on config debug flag if available, else INFO
        log_level = logging.DEBUG if getattr(self.config, "debug", False) else logging.INFO
        self._logger.setLevel(log_level)
        
        # Only add handler if not already present
        if not self._logger.handlers:
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setLevel(log_level)
            formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            file_handler.setFormatter(formatter)
            self._logger.addHandler(file_handler)

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self.market.is_connected

    @property
    def current_market(self) -> Optional[MarketInfo]:
        """Get current market info."""
        return self.market.current_market

    @property
    def token_ids(self) -> Dict[str, str]:
        """Get current token IDs."""
        return self.market.token_ids

    @property
    def current_market_open_orders(self) -> List[OrderEvent]:
        """Get active orders for the current market."""
        token_set = {token_id for token_id in self.token_ids.values() if token_id}
        if not token_set:
            return []
        # Return orders sorted by timestamp descending (newest first)
        orders = [order for order in self._active_orders.values() if order.asset_id in token_set]
        return sorted(orders, key=lambda x: x.timestamp, reverse=True)

    @property
    def has_active_orders(self) -> bool:
        """Fast check if there are any active orders for the current market."""
        token_set = {token_id for token_id in self.token_ids.values() if token_id}
        if not token_set:
            return False
        return any(order.asset_id in token_set for order in self._active_orders.values())

    def _to_float(self, value: object, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def log(self, msg: str, level: str = "info") -> None:
        """
        Log a message.

        Args:
            msg: Message to log
            level: Log level (info, success, warning, error, trade)
        """
        # Map our internal levels to Python logging levels
        py_level = logging.INFO
        if level == "error":
            py_level = logging.ERROR
        elif level == "warning":
            py_level = logging.WARNING
        elif level == "debug":
            py_level = logging.DEBUG
            
        # Log to isolated file logger for this strategy instance
        self._logger.log(py_level, f"[{level.upper()}] {msg}")

        if self._status_mode:
            self._log_buffer.add(msg, level)
        else:
            log(f"[{self.config.coin}] {msg}", level)

    async def start(self) -> bool:
        """
        Start the strategy.

        Returns:
            True if started successfully
        """
        self.running = True

        # Register callbacks on market manager
        @self.market.on_book_update
        async def handle_book(snapshot: OrderbookSnapshot):  # pyright: ignore[reportUnusedFunction]
            mid_prices = self._get_mid_prices()

            # Record mid price for accurate flash crash detection
            for side, token_id in self.token_ids.items():
                if token_id == snapshot.asset_id:
                    mid = mid_prices.get(side, 0)
                    if mid > 0:
                        self.prices.record(side, mid)
                    break

            # Delegate to subclass specific book updates if any
            await self.on_book_update(snapshot)

            # Evaluate strategy logic on price update using mid prices
            await self.on_tick(mid_prices)

            # Evaluate exits only if we have positions
            if self.positions.get_all_positions():
                exit_prices = self._get_exit_prices()
                if exit_prices:
                    self._last_exit_prices = exit_prices
                # Evaluate exits on price update using effective exit prices
                await self._check_exits(exit_prices, mid_prices)

        @self.market.on_market_change
        def handle_market_change(old_slug: str, new_slug: str):  # pyright: ignore[reportUnusedFunction]
            self.log(f"Market changed: {old_slug} -> {new_slug}", "warning")
            
            # Clear any orphaned positions from the old market
            for pos in self.positions.get_all_positions():
                exit_price = self._last_exit_prices.get(pos.side, pos.entry_price)
                realized_pnl = pos.size * (exit_price - pos.entry_price)
                self.log(f"Clearing orphaned position {pos.side.upper()} from old market locally. PnL: ${realized_pnl:+.2f}", "warning")
                self.positions.close_position(pos.id, realized_pnl=realized_pnl)

            self.prices.clear()
            self.on_market_change(old_slug, new_slug)

        @self.market.on_connect
        def handle_connect():  # pyright: ignore[reportUnusedFunction]
            self.log("WebSocket connected", "success")
            self.on_connect()

        @self.market.on_disconnect
        def handle_disconnect():  # pyright: ignore[reportUnusedFunction]
            self.log("WebSocket disconnected", "warning")
            self.on_disconnect()

        # Setup User WebSocket
        await self._setup_user_websocket()

        # Start market manager
        if not await self.market.start():
            self.running = False
            return False

        # Wait for initial data
        if not await self.market.wait_for_data(timeout=5.0):
            self.log("Timeout waiting for market data", "warning")

        return True

    async def _setup_user_websocket(self) -> None:
        """Initialize and start User WebSocket for trades and orders."""
        api_creds = self.bot.api_creds
        if not api_creds:
            self.log("Cannot start User WebSocket: No API credentials available.", "error")
            return

        # Handle py_clob_client_v2's ApiCreds object or our own ApiCredentials class
        try:
            # First try the attributes from our local ApiCredentials class or standard SDK format
            api_key = getattr(api_creds, "api_key", None)
            if not api_key:
                # Some SDK versions might use different casing
                api_key = getattr(api_creds, "apiKey", None)
                
            secret = getattr(api_creds, "secret", None)
            if not secret:
                secret = getattr(api_creds, "api_secret", None)
                
            passphrase = getattr(api_creds, "passphrase", None)
            if not passphrase:
                passphrase = getattr(api_creds, "api_passphrase", None)
                
            if not api_key or not secret or not passphrase:
                # Fallback to dictionary access if it's a dict-like object
                if isinstance(api_creds, dict):
                    api_key = api_creds.get("api_key", api_creds.get("apiKey"))
                    secret = api_creds.get("secret", api_creds.get("api_secret"))
                    passphrase = api_creds.get("passphrase", api_creds.get("api_passphrase"))
                
                if not api_key or not secret or not passphrase:
                    self.log(f"Cannot start User WebSocket: Missing one or more API credentials (type: {type(api_creds)}).", "error")
                    return
        except Exception as e:
            self.log(f"Cannot start User WebSocket: Error accessing API credentials: {e}", "error")
            return

        self.user_ws = UserWebSocket(
            api_key=api_key,
            secret=secret,
            passphrase=passphrase
        )

        @self.user_ws.on_trade
        async def handle_trade(trade: TradeEvent):
            if trade.asset_id not in self.token_ids.values():
                return
            self.log(f"User Trade: {trade.side} {trade.size}@{trade.price} (Order: {trade.order_id})", "trade")

        @self.user_ws.on_order
        async def handle_order(order: OrderEvent):
            if order.asset_id not in self.token_ids.values():
                return
            # Manage local active orders state based on the raw API event
            # A completely filled or canceled order should be removed from active orders.
            # Note: Polymarket order events include status fields like PLACEMENT, UPDATE, CANCELLATION.
            # Consider an order filled if the matched size is at least 97% of the original size
            # to handle precision rounding and tiny partial fills.
            is_filled = order.size_matched >= (order.original_size * 0.97)
            
            if order.status in ["CANCELLATION", "CANCELED", "FAILED"] or (order.status == "UPDATE" and is_filled):
                self._active_orders.pop(order.order_id, None)
            elif order.status in ["PLACEMENT", "UPDATE", "LIVE", "PARTIALLY_MATCHED", "NEW"]:
                self._active_orders[order.order_id] = order

            if order.size_matched > 0:
                self.log(f"User Order Matched: {order.side} {order.size_matched}/{order.original_size}@{order.price} (Order: {order.order_id})", "trade")

            # Handle state transitions for pending positions based on Event-Driven logic
            pos = self.positions.get_position_by_order_id(order.order_id)
            if pos:
                if pos.status == "pending_open" and pos.order_id == order.order_id:
                    if order.status in ["CANCELLATION", "CANCELED", "FAILED"] or (order.status == "UPDATE" and not is_filled and order.original_size == 0):
                        self.log(f"Open order {order.order_id} canceled/failed, removing pending position.", "warning")
                        self.positions.remove_position(pos.id)
                    elif is_filled:
                        self.positions.confirm_open_position(order.order_id, order.price, order.size_matched)
                        self.log(f"Open order {order.order_id} filled, position {pos.side.upper()} is now OPEN.", "success")
                        self.prices.clear()  # clear prices when position is opened to avoid consecutive triggers
                
                elif pos.status == "pending_close" and pos.hedge_order_id == order.order_id:
                    if order.status in ["CANCELLATION", "CANCELED", "FAILED"]:
                        self.log(f"Close order {order.order_id} canceled/failed, reverting position {pos.side.upper()} to OPEN.", "warning")
                        pos.status = "open"
                        pos.hedge_order_id = None
                    elif is_filled:
                        effective_sell_price = 1.0 - order.price
                        realized_pnl = pos.size * (effective_sell_price - pos.entry_price)
                        self.positions.close_position(pos.id, realized_pnl=realized_pnl)
                        self.log(f"Close order {order.order_id} filled, position {pos.side.upper()} CLOSED. PnL: ${realized_pnl:+.2f}", "success")

        self._user_ws_task = asyncio.create_task(self.user_ws.run(auto_reconnect=True))
        self.log("User WebSocket background task started.", "info")

    async def stop(self) -> None:
        """Stop the strategy."""
        self.running = False

        if self.user_ws:
            self.user_ws.stop()
        if self._user_ws_task:
            self._user_ws_task.cancel()
            try:
                await self._user_ws_task
            except asyncio.CancelledError:
                pass
            self._user_ws_task = None

        # Wait for any pending background tasks to complete (like open/close execution)
        if self._background_tasks:
            self.log(f"Waiting for {len(self._background_tasks)} background tasks to complete...", "info")
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

        await self.market.stop()

    async def run(self) -> None:
        """Main strategy loop."""
        try:
            if not await self.start():
                self.log("Failed to start strategy", "error")
                return

            self._status_mode = True

            while self.running:
                # Get current effective prices for UI rendering
                prices = self._get_exit_prices()
                
                # Fallback to mid prices if exit prices are missing/0
                mid_prices = self._get_mid_prices()
                for side in ["up", "down"]:
                    if side not in prices or prices[side] == 0:
                        prices[side] = mid_prices.get(side, 0)

                # Update display
                self.render_status(prices)

                # Force flush output to ensure it renders immediately
                import sys
                sys.stdout.flush()

                # UI render and general maintenance tick (keep 0.1s or make it slightly longer for UI)
                await asyncio.sleep(self.config.update_interval)

        except KeyboardInterrupt:
            self.log("Strategy stopped by user")
        finally:
            await self.stop()
            self._print_summary()

    def _get_mid_prices(self) -> Dict[str, float]:
        """Get current mid prices for entries and flash crash detection."""
        prices = {}
        for side in ["up", "down"]:
            ob = self.market.get_orderbook(side)
            if ob:
                mid = ob.mid_price
                if mid > 0:
                    prices[side] = mid
        return prices

    def _get_exit_prices(self) -> Dict[str, float]:
        """Get effective exit prices (accounting for slippage) for TP/SL evaluation."""
        prices = {}
        for side in ["up", "down"]:
            opposite_side = "down" if side == "up" else "up"
            ob_opposite = self.market.get_orderbook(opposite_side)
            
            if ob_opposite:
                opposite_mid = ob_opposite.mid_price
                if opposite_mid > 0:
                    # 模拟在对面买入对冲的成本：对面mid + slippage
                    buy_price_opposite = min(max(opposite_mid + self.config.slippage, 0.01), 0.99)
                    # 换算回同侧的等效卖出价用于触发判断
                    prices[side] = 1.0 - buy_price_opposite
        return prices

    async def _check_exits(self, exit_prices: Dict[str, float], mid_prices: Dict[str, float]) -> None:
        """Check and execute exits for all positions."""
        # First, remove positions that don't belong to the current market
        for pos in self.positions.get_all_positions():
            current_token_id = self.token_ids.get(pos.side)
            if pos.token_id != current_token_id:
                exit_price = self._last_exit_prices.get(pos.side, pos.entry_price)
                realized_pnl = pos.size * (exit_price - pos.entry_price)
                self.log(f"Position {pos.id} ({pos.side.upper()}) is from a previous market. Clearing locally. PnL: ${realized_pnl:+.2f}", "warning")
                self.positions.close_position(pos.id, realized_pnl=realized_pnl)

        # Check exits against the effective exit price (accounting for slippage), not the raw market mid price
        exits = self.positions.check_all_exits(exit_prices)
        for position, exit_type, pnl in exits:
            if exit_type in ("take_profit", "stop_loss") and position.status == "open":
                # Recalculate actual PnL for logging using the effective exit price
                actual_pnl = position.get_pnl(exit_prices.get(position.side, 0))
                if exit_type == "take_profit":
                    self.log(f"TAKE PROFIT TRIGGERED: {position.side.upper()} PnL: +${actual_pnl:.2f}", "success")
                else:
                    self.log(f"STOP LOSS TRIGGERED: {position.side.upper()} PnL: ${actual_pnl:.2f}", "success")
                
                # Execute close
                await self.execute_close(position, exit_prices.get(position.side, 0))

    async def execute_close(self, position: Position, current_price: float) -> bool:
        """
        Execute close.
        """
        if position.status != "open":
            return False
            
        position.status = "pending_close"
        # 改为后台异步任务，彻底解除对 WebSocket 接收循环的阻塞
        task = asyncio.create_task(self._do_execute_close(position, current_price))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return True

    async def _do_execute_close(self, position: Position, current_price: float) -> None:
        """Actual execution of the close order."""
        try:
            opposite_side = "down" if position.side.lower() == "up" else "up"
            opposite_token_id = self.token_ids.get(opposite_side)
            
            if not opposite_token_id:
                self.log(f"Cannot execute close: No token ID for {opposite_side}", "error")
                position.status = "open"
                return

            ob_opposite = self.market.get_orderbook(opposite_side)
            if not ob_opposite:
                self.log(f"Cannot execute close: No orderbook for {opposite_side}", "error")
                position.status = "open"
                return

            opposite_ask = ob_opposite.best_ask
            if opposite_ask <= 0:
                opposite_ask = ob_opposite.mid_price

            # We buy the opposite side to close out our position.
            # Add slippage to ensure fill
            buy_price_opposite = min(max(opposite_ask + self.config.slippage, 0.01), 0.99)
            buy_price_opposite = round(buy_price_opposite, 4)

            prefix = "DRY RUN: " if self.config.dry_run else ""
            self.log(f"{prefix}Executing Close for {position.side.upper()} by buying {opposite_side.upper()} @ {buy_price_opposite:.4f} size={position.size:.2f} (GTC)", "trade")

            if self.config.dry_run:
                class FakeResult:
                    success = True
                    order_id = f"dry_run_{int(time.time())}"
                    status = "FILLED"
                    data = {"size_matched": position.size, "average_price": buy_price_opposite}
                result = FakeResult()
            else:
                result = await self.bot.place_order(
                    token_id=opposite_token_id,
                    price=buy_price_opposite,
                    size=position.size,
                    side="BUY",
                    order_type="GTC"
                )

            is_killed = result.status in ("CANCELLED", "KILLED") if hasattr(result, "status") else False
            if result.success and result.order_id and not is_killed:
                position.hedge_order_id = result.order_id
                self.log(f"{prefix}Close GTC Order placed: {result.order_id}, waiting for WS confirmation", "success")
                
                if self.config.dry_run:
                    actual_price = buy_price_opposite
                    effective_sell_price = 1.0 - actual_price
                    realized_pnl = position.size * (effective_sell_price - position.entry_price)
                    self.positions.close_position(position.id, realized_pnl=realized_pnl)
            else:
                err_msg = getattr(result, 'message', '') or getattr(result, 'status', '')
                self.log(f"{prefix}Close GTC Order failed/killed: {err_msg}. Reverting to open status.", "error")
                position.status = "open"
                position.hedge_order_id = None
        except Exception as e:
            self.log(f"Exception during close execution: {e}", "error")
            position.status = "open"
            position.hedge_order_id = None

    async def execute_open(self, side: str, mid_price: float) -> bool:
        """
        Execute limit buy order (Maker).
        """
        # Global lock check: Prevent concurrent duplicate triggers across sides
        if getattr(self, "_is_opening", False):
            return False
            
        setattr(self, "_is_opening", True)
        
        # 改为后台异步任务，彻底解除对 WebSocket 接收循环的阻塞
        task = asyncio.create_task(self._do_execute_open(side, mid_price))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return True

    async def _do_execute_open(self, side: str, mid_price: float) -> None:
        """Actual execution of the open order."""
        try:
            token_id = self.token_ids.get(side)
            if not token_id:
                self.log(f"No token ID for {side}", "error")
                return

            # Check if we can still open a position (in case another task just opened one)
            if not self.positions.can_open_position:
                self.log(f"Skip Open: Max positions reached", "warning")
                return

            # Check if there are any active open orders for the current market
            if self.has_active_orders:
                self.log(f"Skip Open: Pending open orders exist", "warning")
                return

            if self.positions.has_position(side):
                self.log(f"Skip Open: Position already exists for {side.upper()}", "warning")
                return

            # Spread Check: Skip if market liquidity is too poor
            current_spread = self.market.get_spread(side)
            if current_spread > self.config.max_spread:
                self.log(f"Skip Open: Spread too wide ({current_spread:.4f} > {self.config.max_spread:.4f})", "warning")
                return

            size = self.config.size
            if self.config.max_size > 0 and size > self.config.max_size:
                self.log(f"Skip Open: size {size:.2f} above max", "warning")
                return
            if mid_price < self.config.min_price or mid_price > self.config.max_price:
                self.log(
                    f"Skip Open: price {mid_price:.4f} outside [{self.config.min_price:.2f}, {self.config.max_price:.2f}]",
                    "warning"
                )
                return
                
            # Order uses mid price + slippage to cross the book
            buy_price = min(max(mid_price + self.config.slippage, 0.01), 0.99)
            buy_price = round(buy_price, 4)

            prefix = "DRY RUN: " if self.config.dry_run else ""
            self.log(f"{prefix}Open {side.upper()} @ {buy_price:.4f} size={size:.2f} (GTC)", "trade")

            # First, register a pending position locally to lock the slot
            pending_pos = self.positions.open_position(
                side=side,
                token_id=token_id,
                entry_price=buy_price,
                size=size,
                status="pending_open"
            )
            
            if not pending_pos:
                self.log("Skip Open: Failed to register pending position", "warning")
                return

            if self.config.dry_run:
                class FakeResult:
                    success = True
                    order_id = f"dry_run_{int(time.time())}"
                    status = "LIVE"
                    data = {"size_matched": 0, "average_price": 0}
                result = FakeResult()
            else:
                result = await self.bot.place_order(
                    token_id=token_id,
                    price=buy_price,
                    size=size,
                    side="BUY",
                    order_type="GTC"
                )

            if result.success and result.order_id:
                # Update pending pos with order_id, wait for WS confirmation to transition to open
                pending_pos.order_id = result.order_id
                self.log(f"{prefix}Open GTC Order placed: {result.order_id} ({size:.2f} @ {buy_price:.4f}), waiting for WS confirmation.", "success")
                
                # If dry_run, manually confirm it
                if self.config.dry_run:
                    self.positions.confirm_open_position(result.order_id, buy_price, size)
                    self.prices.clear()
            else:
                self.log(f"{prefix}Open GTC Order failed: {getattr(result, 'message', '') or getattr(result, 'status', '')}", "error")
                # Clean up the pending position immediately if placement failed
                self.positions.remove_position(pending_pos.id)
        except Exception as e:
            self.log(f"Exception during open execution: {e}", "error")
            # If there was an error and we have a pending pos, maybe remove it
            # But we don't have access to pending_pos safely here if it failed before. We'll rely on timeout/error handling or WS.
        finally:
            # Always release the lock
            setattr(self, "_is_opening", False)

    def _print_summary(self) -> None:
        """Print session summary."""
        self._status_mode = False
        print()
        stats = self.positions.get_stats()
        self.log("Session Summary:")
        self.log(f"  Trades: {stats['trades_closed']}")
        self.log(f"  Total PnL: ${stats['total_pnl']:+.2f}")
        self.log(f"  Win rate: {stats['win_rate']:.1f}%")

    # Abstract methods to implement in subclasses

    @abstractmethod
    async def on_book_update(self, snapshot: OrderbookSnapshot) -> None:
        """
        Handle orderbook update.

        Called when new orderbook data is received.

        Args:
            snapshot: OrderbookSnapshot from WebSocket
        """
        pass

    @abstractmethod
    async def on_tick(self, prices: Dict[str, float]) -> None:
        """
        Handle strategy tick.

        Called on each iteration of the main loop.

        Args:
            prices: Current prices {side: price}
        """
        pass

    def render_status(self, prices: Dict[str, float]) -> None:
        """
        Render status display.

        Called on each tick to update the display.

        Args:
            prices: Current prices
        """
        if hasattr(self, 'get_status_lines'):
            lines = self.get_status_lines(prices)
            output = "\033[H\033[J" + "\n".join(lines)
            print(output, flush=True)

    # Optional hooks (override as needed)

    def on_market_change(self, old_slug: str, new_slug: str) -> None:
        """Called when market changes."""
        pass

    def on_connect(self) -> None:
        """Called when WebSocket connects."""
        pass

    def on_disconnect(self) -> None:
        """Called when WebSocket disconnects."""
        pass
