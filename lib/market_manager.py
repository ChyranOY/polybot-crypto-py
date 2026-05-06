"""
Market Manager - Market Discovery and WebSocket Management

Provides unified interface for:
- Market discovery via GammaClient
- WebSocket connection and subscription management
- Automatic market switching when markets expire
- Real-time orderbook caching

Usage:
    from lib import MarketManager

    manager = MarketManager(coin="BTC")

    @manager.on_book_update
    async def handle_book(snapshot):
        print(f"Mid price: {snapshot.mid_price}")

    @manager.on_market_change
    def handle_change(old_slug, new_slug):
        print(f"Market changed!")

    await manager.start()
    await manager.wait_for_data()

    # Access data
    ob = manager.get_orderbook("up")
    print(f"Best bid: {ob.best_bid}")

    await manager.stop()
"""

import asyncio
import time
import random
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, Dict, Callable, List, Union, Awaitable, Any, Set

from src.gamma_client import GammaClient
from src.websocket_client import MarketWebSocket, OrderbookSnapshot


@dataclass
class MarketInfo:
    """Current market information."""

    slug: str
    question: str
    end_date: str
    token_ids: Dict[str, str]
    prices: Dict[str, float]
    accepting_orders: bool

    @property
    def up_token(self) -> str:
        """Get UP token ID."""
        return self.token_ids.get("up", "")

    @property
    def down_token(self) -> str:
        """Get DOWN token ID."""
        return self.token_ids.get("down", "")

    def get_countdown(self) -> tuple[int, int]:
        """
        Get countdown until market ends.

        Returns:
            Tuple of (minutes, seconds), or (-1, -1) if unavailable
        """
        if not self.end_date:
            return (-1, -1)

        try:
            end_date_str = self.end_date.replace("Z", "+00:00")
            end_time = datetime.fromisoformat(end_date_str)
            now = datetime.now(timezone.utc)
            remaining = end_time - now

            if remaining.total_seconds() <= 0:
                return (0, 0)

            total_secs = int(remaining.total_seconds())
            return (total_secs // 60, total_secs % 60)
        except Exception:
            return (-1, -1)

    def get_countdown_str(self) -> str:
        """Get formatted countdown string (MM:SS)."""
        mins, secs = self.get_countdown()
        if mins < 0:
            return "--:--"
        if mins == 0 and secs == 0:
            return "ENDED"
        return f"{mins:02d}:{secs:02d}"

    def slug_timestamp(self) -> Optional[int]:
        """Extract timestamp suffix from slug if present."""
        if not self.slug:
            return None
        ts = self.slug.split("-")[-1]
        if not ts.isdigit():
            return None
        try:
            return int(ts)
        except ValueError:
            return None

    def end_timestamp(self) -> Optional[int]:
        """Parse end_date into epoch seconds if available."""
        if not self.end_date:
            return None
        try:
            end_date_str = self.end_date.replace("Z", "+00:00")
            return int(datetime.fromisoformat(end_date_str).timestamp())
        except Exception:
            return None

    def is_ending_soon(self, threshold_seconds: int = 60) -> bool:
        """Check if market is ending within threshold."""
        mins, secs = self.get_countdown()
        if mins < 0:
            return False
        return (mins * 60 + secs) <= threshold_seconds

    def has_ended(self) -> bool:
        """Check if market has ended."""
        mins, secs = self.get_countdown()
        return mins == 0 and secs == 0


# Callback type aliases
BookCallback = Callable[[OrderbookSnapshot], Union[None, Awaitable[None]]]
MarketChangeCallback = Callable[[str, str], None]  # (old_slug, new_slug)
ConnectionCallback = Callable[[], None]


class MarketManager:
    """
    Manages market discovery and WebSocket connections.

    Provides:
    - Automatic market discovery
    - WebSocket connection with auto-reconnect
    - Market change detection and notification
    - Orderbook caching
    """

    def __init__(
        self,
        coin: str = "BTC",
        market_interval: str = "5m",
        market_check_interval: float = 10.0,
        auto_switch_market: bool = True,
        max_upcoming_markets: int = 2,
    ):
        """
        Initialize market manager.

        Args:
            coin: Coin symbol (BTC, ETH, SOL, XRP)
            market_check_interval: Seconds between market checks
            auto_switch_market: Auto switch when market changes
        """
        self.coin = coin.upper()
        self.market_interval = market_interval
        self.market_check_interval = market_check_interval
        self.auto_switch_market = auto_switch_market
        self.max_upcoming_markets = max_upcoming_markets

        # Clients
        self.gamma = GammaClient()
        self.ws: Optional[MarketWebSocket] = None

        # State
        self.current_market: Optional[MarketInfo] = None
        self.upcoming_markets: List[MarketInfo] = []
        self._previous_slug: Optional[str] = None
        self._known_slugs: Set[str] = set()
        self._running = False
        self._ws_connected = False
        self._ws_task: Optional[asyncio.Task] = None
        self._market_check_task: Optional[asyncio.Task] = None
        self._reconnect_lock: Optional[asyncio.Lock] = None

        # Callbacks
        self._on_book_callbacks: List[BookCallback] = []
        self._on_market_change_callbacks: List[MarketChangeCallback] = []
        self._on_connect_callbacks: List[ConnectionCallback] = []
        self._on_disconnect_callbacks: List[ConnectionCallback] = []

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self._ws_connected

    @property
    def is_running(self) -> bool:
        """Check if manager is running."""
        return self._running

    @property
    def token_ids(self) -> Dict[str, str]:
        """Get current market token IDs."""
        if self.current_market:
            return self.current_market.token_ids
        return {}

    def get_orderbook(self, side: str) -> Optional[OrderbookSnapshot]:
        """
        Get cached orderbook for side.

        Args:
            side: "up" or "down"

        Returns:
            OrderbookSnapshot or None
        """
        if not self.ws or not self.current_market:
            return None
        token_id = self.current_market.token_ids.get(side)
        if token_id:
            return self.ws.get_orderbook(token_id)
        return None

    def get_mid_price(self, side: str) -> float:
        """Get mid price for side."""
        ob = self.get_orderbook(side)
        return ob.mid_price if ob else 0.0

    def get_best_bid(self, side: str) -> float:
        """Get best bid price for side."""
        ob = self.get_orderbook(side)
        return ob.best_bid if ob else 0.0

    def get_best_ask(self, side: str) -> float:
        """Get best ask price for side."""
        ob = self.get_orderbook(side)
        return ob.best_ask if ob else 1.0

    def get_spread(self, side: str) -> float:
        """Get spread for side."""
        ob = self.get_orderbook(side)
        if ob and ob.best_bid > 0:
            return ob.best_ask - ob.best_bid
        return 0.0

    # Callback decorators
    def on_book_update(self, callback: BookCallback) -> BookCallback:
        """Register book update callback."""
        self._on_book_callbacks.append(callback)
        return callback

    def on_market_change(self, callback: MarketChangeCallback) -> MarketChangeCallback:
        """Register market change callback."""
        self._on_market_change_callbacks.append(callback)
        return callback

    def on_connect(self, callback: ConnectionCallback) -> ConnectionCallback:
        """Register connect callback."""
        self._on_connect_callbacks.append(callback)
        return callback

    def on_disconnect(self, callback: ConnectionCallback) -> ConnectionCallback:
        """Register disconnect callback."""
        self._on_disconnect_callbacks.append(callback)
        return callback

    def _update_current_market(self, market: MarketInfo) -> None:
        """Update current market state."""
        self._previous_slug = market.slug
        self.current_market = market

    def _market_sort_key(self, market: MarketInfo) -> Optional[int]:
        """Get comparable timestamp for market ordering."""
        return market.slug_timestamp() or market.end_timestamp()

    def _should_switch_market(
        self,
        old_market: Optional[MarketInfo],
        new_market: MarketInfo
    ) -> bool:
        """Check if new market should replace current market."""
        if not old_market:
            return True

        old_tokens = set(old_market.token_ids.values())
        new_tokens = set(new_market.token_ids.values())
        if new_tokens == old_tokens:
            return False

        old_key = self._market_sort_key(old_market)
        new_key = self._market_sort_key(new_market)
        
        # We should only switch to a *newer* market.
        # If new_key <= old_key, it means the new market is actually older or the same,
        # which shouldn't happen unless the API returns weird data.
        if old_key is not None and new_key is not None and new_key <= old_key:
            return False
            
        # Add a check: if the old market is still accepting orders AND hasn't reached its actual end time,
        # we probably shouldn't switch yet.
        now = datetime.now(timezone.utc).timestamp()
        old_end = old_market.end_timestamp()
        
        if old_market.accepting_orders and old_end is not None and now < old_end:
            # We strictly enforce that the old market must be over (or within 2 seconds of ending)
            # before we switch to the new one.
            if (old_end - now) > 2:
                return False

        return True

    def discover_market(self, update_state: bool = True) -> Optional[MarketInfo]:
        """
        Discover current market.

        Returns:
            MarketInfo if found, None otherwise
        """
        market_data = self.gamma.get_market_info(self.coin, interval=self.market_interval)

        if not market_data:
            return None

        if not market_data.get("accepting_orders", False):
            return None

        market = MarketInfo(
            slug=market_data.get("slug", ""),
            question=market_data.get("question", ""),
            end_date=market_data.get("end_date", ""),
            token_ids=market_data.get("token_ids", {}),
            prices=market_data.get("prices", {}),
            accepting_orders=market_data.get("accepting_orders", False),
        )

        if update_state:
            # Note: Market change callbacks are fired in _market_check_loop
            # to ensure they run in the main thread after resubscription
            self._update_current_market(market)
        return market

    def _build_market_info(self, market_data: Dict[str, Any]) -> Optional[MarketInfo]:
        if not market_data:
            return None
        accepting_orders = market_data.get("acceptingOrders", market_data.get("accepting_orders", False))
        if not accepting_orders:
            return None
        token_ids = market_data.get("token_ids") or self.gamma.parse_token_ids(market_data)
        prices = market_data.get("prices") or self.gamma.parse_prices(market_data)
        return MarketInfo(
            slug=market_data.get("slug", ""),
            question=market_data.get("question", ""),
            end_date=market_data.get("endDate", market_data.get("end_date", "")),
            token_ids=token_ids,
            prices=prices,
            accepting_orders=accepting_orders,
        )

    def discover_upcoming_market(self) -> Optional[MarketInfo]:
        market_data = self.gamma.get_next_market(self.coin, interval=self.market_interval)
        return self._build_market_info(market_data)

    async def _setup_websocket(self) -> bool:
        """Setup WebSocket connection and callbacks."""
        if not self.current_market:
            return False

        self.ws = MarketWebSocket()

        @self.ws.on_book
        async def handle_book(snapshot: OrderbookSnapshot):  # pyright: ignore[reportUnusedFunction]
            for callback in self._on_book_callbacks:
                try:
                    result = callback(snapshot)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass

        @self.ws.on_price_change
        async def handle_price_change(market: str, changes: List[Any]):  # pyright: ignore[reportUnusedFunction]
            updated_assets = set()
            for pc in changes:
                if not pc.asset_id:
                    continue
                updated_assets.add(pc.asset_id)
                
            for asset_id in updated_assets:
                snapshot = self.ws.get_orderbook(asset_id)
                if snapshot:
                    for callback in self._on_book_callbacks:
                        try:
                            result = callback(snapshot)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception:
                            pass

        @self.ws.on_connect
        def handle_connect():  # pyright: ignore[reportUnusedFunction]
            self._ws_connected = True
            for callback in self._on_connect_callbacks:
                try:
                    callback()
                except Exception:
                    pass

        @self.ws.on_disconnect
        def handle_disconnect():  # pyright: ignore[reportUnusedFunction]
            self._ws_connected = False
            for callback in self._on_disconnect_callbacks:
                try:
                    callback()
                except Exception:
                    pass

        token_list = []
        for market in self.get_all_markets():
            token_list.extend(market.token_ids.values())
            if market.slug:
                self._known_slugs.add(market.slug)
        if token_list:
            await self.ws.subscribe(token_list, replace=True)

        return True

    async def _run_websocket(self) -> None:
        """Run WebSocket with auto-reconnect."""
        if self.ws:
            await self.ws.run(auto_reconnect=True)

    async def _market_check_loop(self) -> None:
        """Periodically check for market changes."""
        while self._running:
            jitter = random.uniform(0, 5)
            await asyncio.sleep(self.market_check_interval + jitter)

            if not self._running:
                break

            if self._reconnect_lock is None:
                self._reconnect_lock = asyncio.Lock()

            async with self._reconnect_lock:
                old_market = self.current_market
                old_tokens = set(old_market.token_ids.values()) if old_market else set()
                old_slug = old_market.slug if old_market else None

                # Run synchronous HTTP call in thread pool to avoid blocking
                market = await asyncio.to_thread(self.discover_market, update_state=False)

                if not market:
                    continue

                # Check if market changed and resubscribe
                new_tokens = set(market.token_ids.values())
                if new_tokens == old_tokens:
                    self._update_current_market(market)
                    await self._refresh_upcoming_markets()
                    continue

                if not (self.auto_switch_market and self.ws):
                    self._update_current_market(market)
                    await self._refresh_upcoming_markets()
                    continue

                if not self._should_switch_market(old_market, market):
                    await self._refresh_upcoming_markets()
                    continue

                self._update_current_market(market)
                self.upcoming_markets = [m for m in self.upcoming_markets if m.slug != market.slug]

                already_subscribed = all(
                    token in (self.ws._subscribed_assets if self.ws else set())
                    for token in new_tokens
                )

                if not already_subscribed and self.ws:
                    await self.ws.subscribe(self._get_all_subscribed_tokens(), replace=True)

                # Fire market change callbacks in main thread
                if old_slug and old_slug != market.slug:
                    for callback in self._on_market_change_callbacks:
                        try:
                            callback(old_slug, market.slug)
                        except Exception:
                            pass

                await self._refresh_upcoming_markets()

    async def start(self) -> bool:
        """
        Start market manager.

        Returns:
            True if started successfully
        """
        self._running = True

        # Discover initial market (run in thread to avoid blocking event loop)
        market = await asyncio.to_thread(self.discover_market)
        if not market:
            self._running = False
            return False
            
        self.upcoming_markets = []
        upcoming = await asyncio.to_thread(self.discover_upcoming_market)
        if upcoming:
            self.upcoming_markets.append(upcoming)
            
        if self.current_market:
            self._known_slugs.add(self.current_market.slug)
        for market in self.upcoming_markets:
            self._known_slugs.add(market.slug)

        # Setup WebSocket
        if not await self._setup_websocket():
            self._running = False
            return False

        # Start WebSocket in background
        self._ws_task = asyncio.create_task(self._run_websocket())

        # Start market check loop
        if self.auto_switch_market:
            self._market_check_task = asyncio.create_task(self._market_check_loop())

        return True

    async def stop(self) -> None:
        """Stop market manager and cleanup."""
        self._running = False

        if self._market_check_task:
            self._market_check_task.cancel()
            try:
                await self._market_check_task
            except asyncio.CancelledError:
                pass
            self._market_check_task = None

        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None

        if self.ws:
            await self.ws.disconnect()
            self.ws = None

        self._ws_connected = False

    async def wait_for_data(self, timeout: float = 5.0) -> bool:
        """
        Wait for WebSocket to connect and receive data.

        Args:
            timeout: Maximum seconds to wait

        Returns:
            True if connected and received data
        """
        start = time.time()
        while time.time() - start < timeout:
            if self._ws_connected:
                if self.get_orderbook("up") or self.get_orderbook("down"):
                    return True
            await asyncio.sleep(0.1)
        return False

    async def refresh_market(self) -> Optional[MarketInfo]:
        """
        Force refresh market discovery and resubscribe.

        Returns:
            New MarketInfo if found
        """
        old_market = self.current_market
        old_tokens = set(old_market.token_ids.values()) if old_market else set()

        market = await asyncio.to_thread(self.discover_market, update_state=False)
        if not market:
            return None

        new_tokens = set(market.token_ids.values())
        if new_tokens == old_tokens:
            self._update_current_market(market)
            await self._refresh_upcoming_markets()
            return self.current_market

        if not self._should_switch_market(old_market, market):
            await self._refresh_upcoming_markets()
            return old_market

        self._update_current_market(market)
        self.upcoming_markets = [m for m in self.upcoming_markets if m.slug != market.slug]
        if self.ws:
            await self.ws.subscribe(self._get_all_subscribed_tokens(), replace=True)
        await self._refresh_upcoming_markets()
        return market

    def get_all_markets(self) -> List[MarketInfo]:
        markets = []
        if self.current_market:
            markets.append(self.current_market)
        markets.extend(self.upcoming_markets)
        return markets

    def _get_all_subscribed_tokens(self) -> List[str]:
        tokens: List[str] = []
        for market in self.get_all_markets():
            tokens.extend(market.token_ids.values())
        return tokens

    async def _refresh_upcoming_markets(self) -> None:
        upcoming = await asyncio.to_thread(self.discover_upcoming_market)
        if not upcoming:
            return
        if self.current_market and upcoming.slug == self.current_market.slug:
            return
        if upcoming.slug in self._known_slugs:
            return
        self._known_slugs.add(upcoming.slug)
        self.upcoming_markets.append(upcoming)
        self.upcoming_markets.sort(key=self._market_sort_key)
        if len(self.upcoming_markets) > self.max_upcoming_markets:
            self.upcoming_markets = self.upcoming_markets[: self.max_upcoming_markets]
        if self.ws:
            asyncio.create_task(self.ws.subscribe_more(list(upcoming.token_ids.values())))
