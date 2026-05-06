"""
Polymarket Arbitrage Bot - Real-time WebSocket Client

Provides WebSocket connectivity to the Polymarket CLOB API for real-time
market data streaming. This module enables low-latency orderbook updates,
price change notifications, and trade event monitoring.

Features:
    - Real-time orderbook updates with snapshot management
    - Automatic reconnection with exponential backoff
    - Price change notifications and mid-price tracking
    - Trade event monitoring
    - Multi-asset subscription support
    - Cached orderbook access for fast lookups

WebSocket Connection:
    - URL: wss://ws-subscriptions-clob.polymarket.com/ws/market
    - Protocol: JSON-RPC over WebSocket
    - Heartbeat: Automatic ping/pong to maintain connection
    - Reconnection: Automatic with exponential backoff on failures

Example:
    from src.websocket_client import MarketWebSocket, OrderbookSnapshot

    async def on_book_update(snapshot: OrderbookSnapshot):
        print(f"Asset: {snapshot.asset_id}")
        print(f"Mid price: {snapshot.mid_price:.4f}")
        print(f"Best bid: {snapshot.best_bid:.4f}")
        print(f"Best ask: {snapshot.best_ask:.4f}")

    # Initialize and connect
    ws = MarketWebSocket()
    ws.on_book = on_book_update

    # Subscribe to assets
    await ws.subscribe(["token_id_1", "token_id_2"])

    # Start WebSocket connection (blocks until disconnected)
    await ws.run()

    # Or run with auto-reconnect
    await ws.run(auto_reconnect=True)

Note:
    The WebSocket client automatically handles connection management,
    message parsing, and orderbook state synchronization. Callbacks
    are executed asynchronously and should not block for extended periods.
"""

import json
import asyncio
import logging
import os
import time
from typing import Optional, Dict, Any, List, Callable, Set, Union, Awaitable, TYPE_CHECKING
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from websockets.client import WebSocketClientProtocol

logger = logging.getLogger(__name__)


# WebSocket endpoints
WSS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WSS_USER_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

@dataclass
class TradeEvent:
    """User trade event."""
    asset_id: str
    event_type: str
    order_id: str
    outcome: str
    price: float
    side: str
    size: float
    status: str
    timestamp: int
    maker_orders: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_message(cls, msg: Dict[str, Any]) -> "TradeEvent":
        return cls(
            asset_id=msg.get("asset_id", ""),
            event_type=msg.get("event_type", "trade"),
            order_id=msg.get("id", ""),
            outcome=msg.get("outcome", ""),
            price=float(msg.get("price", 0)),
            side=msg.get("side", ""),
            size=float(msg.get("size", 0)),
            status=msg.get("status", ""),
            timestamp=int(msg.get("timestamp", 0)),
            maker_orders=msg.get("maker_orders", [])
        )

@dataclass
class OrderEvent:
    """User order event."""
    asset_id: str
    event_type: str
    order_id: str
    outcome: str
    price: float
    side: str
    size_matched: float
    original_size: float
    status: str
    timestamp: int

    @classmethod
    def from_message(cls, msg: Dict[str, Any]) -> "OrderEvent":
        # The user channel order event types are often PLACEMENT, UPDATE, CANCELLATION
        # Status might be inside the message as well
        return cls(
            asset_id=msg.get("asset_id", ""),
            event_type=msg.get("event_type", "order"),
            order_id=msg.get("id", ""),
            outcome=msg.get("outcome", ""),
            price=float(msg.get("price", 0)),
            side=msg.get("side", ""),
            size_matched=float(msg.get("size_matched", 0)),
            original_size=float(msg.get("original_size", 0)),
            status=msg.get("type", msg.get("status", "")), # Polymarket uses 'type' for PLACEMENT/UPDATE/CANCELLATION
            timestamp=int(msg.get("timestamp", 0))
        )



def _load_websockets():
    """Resolve WebSocket client functions without importing legacy APIs."""
    try:
        from websockets.asyncio.client import connect as ws_connect
        from websockets.exceptions import ConnectionClosed
        return ws_connect, ConnectionClosed
    except ImportError:
        try:
            import websockets
            return websockets.connect, websockets.exceptions.ConnectionClosed
        except ImportError:
            return None, Exception


@dataclass
class OrderbookLevel:
    """Single level in the orderbook."""
    price: float
    size: float


@dataclass
class OrderbookSnapshot:
    """Complete orderbook snapshot."""
    asset_id: str
    market: str
    timestamp: int
    bids: List[OrderbookLevel] = field(default_factory=list)
    asks: List[OrderbookLevel] = field(default_factory=list)
    hash: str = ""

    @property
    def best_bid(self) -> float:
        """Get best bid price."""
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        """Get best ask price."""
        return self.asks[0].price if self.asks else 1.0

    @property
    def mid_price(self) -> float:
        """Get mid price weighted by top level size (shares)."""
        bb = self.best_bid
        ba = self.best_ask
        
        # Clean out invalid best_bid/ask that might come from empty books
        if bb > 0 and ba < 1:
            bid_size = self.bids[0].size if self.bids else 0
            ask_size = self.asks[0].size if self.asks else 0
            total_size = bid_size + ask_size
            
            if total_size > 0:
                # Volume weighted mid price: (BidPrice * AskSize + AskPrice * BidSize) / TotalSize
                # Note: Higher ask size pulls mid price closer to bid, higher bid size pulls mid price closer to ask.
                return (bb * ask_size + ba * bid_size) / total_size
            else:
                return (bb + ba) / 2
        elif bb > 0:
            return bb
        elif ba < 1:
            return ba
        return 0.0

    @classmethod
    def from_message(cls, msg: Dict[str, Any]) -> "OrderbookSnapshot":
        """Create from WebSocket book message."""
        bids = [
            OrderbookLevel(price=float(b["price"]), size=float(b["size"]))
            for b in msg.get("bids", [])
        ]
        asks = [
            OrderbookLevel(price=float(a["price"]), size=float(a["size"]))
            for a in msg.get("asks", [])
        ]
        # Sort bids descending, asks ascending
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        return cls(
            asset_id=msg.get("asset_id", ""),
            market=msg.get("market", ""),
            timestamp=int(msg.get("timestamp", 0)),
            bids=bids,
            asks=asks,
            hash=msg.get("hash", ""),
        )


@dataclass
class PriceChange:
    """Price change event."""
    asset_id: str
    price: float
    size: float
    side: str
    best_bid: float
    best_ask: float
    hash: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PriceChange":
        """Create from price_change dict."""
        return cls(
            asset_id=data.get("asset_id", ""),
            price=float(data.get("price", 0)),
            size=float(data.get("size", 0)),
            side=data.get("side", ""),
            best_bid=float(data.get("best_bid", 0)),
            best_ask=float(data.get("best_ask", 1)),
            hash=data.get("hash", ""),
        )


@dataclass
class LastTradePrice:
    """Last trade price event."""
    asset_id: str
    market: str
    price: float
    size: float
    side: str
    timestamp: int
    fee_rate_bps: int = 0

    @classmethod
    def from_message(cls, msg: Dict[str, Any]) -> "LastTradePrice":
        """Create from last_trade_price message."""
        return cls(
            asset_id=msg.get("asset_id", ""),
            market=msg.get("market", ""),
            price=float(msg.get("price", 0)),
            size=float(msg.get("size", 0)),
            side=msg.get("side", ""),
            timestamp=int(msg.get("timestamp", 0)),
            fee_rate_bps=int(msg.get("fee_rate_bps", 0)),
        )


# Type aliases for callbacks
BookCallback = Callable[[OrderbookSnapshot], Union[None, Awaitable[None]]]
PriceChangeCallback = Callable[[str, List[PriceChange]], Union[None, Awaitable[None]]]
TradeCallback = Callable[[LastTradePrice], Union[None, Awaitable[None]]]
UserTradeCallback = Callable[[TradeEvent], Union[None, Awaitable[None]]]
UserOrderCallback = Callable[[OrderEvent], Union[None, Awaitable[None]]]
ErrorCallback = Callable[[Exception], None]


class MarketWebSocket:
    """
    WebSocket client for Polymarket market data.

    Provides real-time updates for:
    - Orderbook snapshots (book events)
    - Price changes (price_change events)
    - Last trade prices (last_trade_price events)

    Example:
        ws = MarketWebSocket()

        @ws.on_book
        async def handle_book(snapshot: OrderbookSnapshot):
            print(f"Mid price: {snapshot.mid_price}")

        await ws.subscribe(["token_id_1", "token_id_2"])
        await ws.run()
    """

    def __init__(
        self,
        url: str = WSS_MARKET_URL,
        reconnect_interval: float = 5.0,
        ping_interval: float = 20.0,
        ping_timeout: float = 10.0,
    ):
        """
        Initialize WebSocket client.

        Args:
            url: WebSocket endpoint URL
            reconnect_interval: Seconds between reconnection attempts
            ping_interval: Seconds between ping messages
            ping_timeout: Seconds to wait for pong response
        """
        self.url = url
        self.reconnect_interval = reconnect_interval
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout

        self._ws_connect, self._connection_closed = _load_websockets()

        # Connection state
        self._ws: Optional["WebSocketClientProtocol"] = None
        self._running = False
        self._subscribed_assets: Set[str] = set()

        # Orderbook cache
        self._orderbooks: Dict[str, OrderbookSnapshot] = {}

        # Callbacks
        self._on_book: Optional[BookCallback] = None
        self._on_price_change: Optional[PriceChangeCallback] = None
        self._on_trade: Optional[TradeCallback] = None
        self._on_error: Optional[ErrorCallback] = None
        self._on_connect: Optional[Callable[[], None]] = None
        self._on_disconnect: Optional[Callable[[], None]] = None

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        if self._ws is None:
            return False
        # websockets >= 12.0 uses .state, older versions use .open
        try:
            # Try newer API first
            from websockets.protocol import State
            return self._ws.state == State.OPEN
        except (ImportError, AttributeError):
            # Fallback to older API
            try:
                return self._ws.open
            except AttributeError:
                return False

    @property
    def orderbooks(self) -> Dict[str, OrderbookSnapshot]:
        """Get cached orderbooks."""
        return self._orderbooks

    def get_orderbook(self, asset_id: str) -> Optional[OrderbookSnapshot]:
        """Get cached orderbook for asset."""
        return self._orderbooks.get(asset_id)

    def get_mid_price(self, asset_id: str) -> float:
        """Get mid price for asset."""
        ob = self._orderbooks.get(asset_id)
        return ob.mid_price if ob else 0.0

    # Callback decorators
    def on_book(self, callback: BookCallback) -> BookCallback:
        """Decorator to set book update callback."""
        self._on_book = callback
        return callback

    def on_price_change(self, callback: PriceChangeCallback) -> PriceChangeCallback:
        """Decorator to set price change callback."""
        self._on_price_change = callback
        return callback

    def on_trade(self, callback: TradeCallback) -> TradeCallback:
        """Decorator to set trade callback."""
        self._on_trade = callback
        return callback

    def on_error(self, callback: ErrorCallback) -> ErrorCallback:
        """Decorator to set error callback."""
        self._on_error = callback
        return callback

    def on_connect(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Decorator to set connect callback."""
        self._on_connect = callback
        return callback

    def on_disconnect(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Decorator to set disconnect callback."""
        self._on_disconnect = callback
        return callback

    async def connect(self) -> bool:
        """
        Connect to WebSocket.

        Returns:
            True if connected successfully
        """
        try:
            if self._ws_connect is None:
                raise RuntimeError("websockets is not installed")

            original_env = {}
            proxy_vars = ["http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]
            for var in proxy_vars:
                if var in os.environ:
                    original_env[var] = os.environ[var]
                    del os.environ[var]
            try:
                self._ws = await self._ws_connect(
                    self.url,
                    ping_interval=self.ping_interval,
                    ping_timeout=self.ping_timeout,
                )
            finally:
                for var, value in original_env.items():
                    os.environ[var] = value
            logger.info(f"WebSocket connected to {self.url}")
            if self._on_connect:
                self._on_connect()
            return True
        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            if self._on_error:
                self._on_error(e)
            return False

    async def disconnect(self) -> None:
        """Disconnect from WebSocket."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
            logger.info("WebSocket disconnected")
            if self._on_disconnect:
                self._on_disconnect()

    async def subscribe(self, asset_ids: List[str], replace: bool = False) -> bool:
        """
        Subscribe to market data for assets.

        Args:
            asset_ids: List of token IDs to subscribe to
            replace: If True, replace existing subscriptions (clears old data)

        Returns:
            True if subscription sent successfully
        """
        if not asset_ids:
            return False

        if replace:
            # Clear old subscriptions and cached data
            self._subscribed_assets.clear()
            self._orderbooks.clear()

        self._subscribed_assets.update(asset_ids)
        logger.info(f"subscribe() called with {len(asset_ids)} assets, is_connected={self.is_connected}, ws={self._ws is not None}")

        if not self.is_connected:
            # Will subscribe after connect
            logger.info("Not connected yet, will subscribe after connect")
            return True

        subscribe_msg = {
            "assets_ids": asset_ids,
            "type": "MARKET",
        }

        try:
            msg_json = json.dumps(subscribe_msg)
            logger.info(f"Sending subscribe message: {msg_json[:200]}")
            await self._ws.send(msg_json)
            logger.info(f"Subscribed to {len(asset_ids)} assets successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to subscribe: {e}")
            if self._on_error:
                self._on_error(e)
            return False

    async def subscribe_more(self, asset_ids: List[str]) -> bool:
        """
        Subscribe to additional assets.

        Args:
            asset_ids: Additional token IDs to subscribe to

        Returns:
            True if subscription sent successfully
        """
        if not asset_ids:
            return False

        self._subscribed_assets.update(asset_ids)

        if not self.is_connected:
            return True

        subscribe_msg = {
            "assets_ids": asset_ids,
            "operation": "subscribe",
        }

        try:
            await self._ws.send(json.dumps(subscribe_msg))
            logger.info(f"Subscribed to {len(asset_ids)} additional assets")
            return True
        except Exception as e:
            logger.error(f"Failed to subscribe: {e}")
            return False

    async def unsubscribe(self, asset_ids: List[str]) -> bool:
        """
        Unsubscribe from assets.

        Args:
            asset_ids: Token IDs to unsubscribe from

        Returns:
            True if unsubscription sent successfully
        """
        if not self.is_connected or not asset_ids:
            return False

        self._subscribed_assets.difference_update(asset_ids)

        unsubscribe_msg = {
            "assets_ids": asset_ids,
            "operation": "unsubscribe",
        }

        try:
            await self._ws.send(json.dumps(unsubscribe_msg))
            logger.info(f"Unsubscribed from {len(asset_ids)} assets")
            return True
        except Exception as e:
            logger.error(f"Failed to unsubscribe: {e}")
            return False

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Handle incoming WebSocket message."""
        event_type = data.get("event_type", "")
        logger.debug(f"Received event: {event_type}, keys: {list(data.keys())}")

        if event_type == "book":
            snapshot = OrderbookSnapshot.from_message(data)
            self._orderbooks[snapshot.asset_id] = snapshot
            logger.debug(f"Book update for {snapshot.asset_id[:20]}...: mid={snapshot.mid_price:.4f}")
            await self._run_callback(self._on_book, snapshot, label="book")

        elif event_type == "price_change":
            market = data.get("market", "")
            changes = [
                PriceChange.from_dict(pc)
                for pc in data.get("price_changes", [])
            ]
            for pc in changes:
                self._update_orderbook_from_price_change(pc, market)
            await self._run_callback(
                self._on_price_change,
                market,
                changes,
                label="price_change",
            )

        elif event_type == "last_trade_price":
            trade = LastTradePrice.from_message(data)
            await self._run_callback(self._on_trade, trade, label="trade")

        elif event_type == "tick_size_change":
            # Log but don't handle specially
            logger.debug(f"Tick size change: {data}")

        else:
            logger.debug(f"Unknown event type: {event_type}")

    async def _run_callback(self, callback: Optional[Callable[..., Any]], *args: Any, label: str) -> None:
        """Run a callback that may be sync or async, logging failures."""
        if not callback:
            return
        try:
            result = callback(*args)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.error(f"Error in {label} callback: {e}")

    def _update_orderbook_from_price_change(self, pc: PriceChange, market: str) -> None:
        asset_id = pc.asset_id
        if not asset_id:
            return
        existing = self._orderbooks.get(asset_id)
        if not existing:
            return

        existing.timestamp = int(time.time() * 1000)
        existing.market = market or existing.market

        # Update the specific price level
        if pc.side.upper() == "BUY":
            # Update bids
            target_list = existing.bids
        elif pc.side.upper() == "SELL":
            # Update asks
            target_list = existing.asks
        else:
            return

        # Find if the price level already exists
        level_idx = -1
        for i, level in enumerate(target_list):
            if abs(level.price - pc.price) < 1e-8:
                level_idx = i
                break

        if pc.size <= 0:
            # Remove level if size is 0
            if level_idx != -1:
                target_list.pop(level_idx)
        else:
            # Add or update level
            if level_idx != -1:
                target_list[level_idx].size = pc.size
            else:
                target_list.append(OrderbookLevel(price=pc.price, size=pc.size))

        # Re-sort the lists (bids descending, asks ascending)
        if pc.side.upper() == "BUY":
            existing.bids.sort(key=lambda x: x.price, reverse=True)
        else:
            existing.asks.sort(key=lambda x: x.price)
            
        # Re-eval best bid/ask
        if existing.bids:
            pc.best_bid = existing.bids[0].price
        if existing.asks:
            pc.best_ask = existing.asks[0].price

    async def _run_loop(self) -> None:
        """Main message processing loop."""
        msg_count = 0
        while self._running and self.is_connected:
            try:
                message = await asyncio.wait_for(
                    self._ws.recv(),
                    timeout=self.ping_interval + 5
                )
                msg_count += 1

                # Log first 5 messages, then every 1000
                if msg_count <= 5 or msg_count % 1000 == 0:
                    logger.info(f"WS message #{msg_count}: {message[:200] if len(message) > 200 else message}")

                data = json.loads(message)

                # Handle array of messages
                if isinstance(data, list):
                    for item in data:
                        await self._handle_message(item)
                else:
                    await self._handle_message(data)

            except asyncio.TimeoutError:
                logger.warning("WebSocket receive timeout")
            except self._connection_closed as e:
                logger.warning(f"WebSocket connection closed: {e}")
                break
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse message: {e}")
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                if self._on_error:
                    self._on_error(e)

    async def run(self, auto_reconnect: bool = True) -> None:
        """
        Run the WebSocket client.

        Args:
            auto_reconnect: Whether to automatically reconnect on disconnect
        """
        self._running = True

        while self._running:
            # Connect
            if not await self.connect():
                if auto_reconnect:
                    logger.info(f"Reconnecting in {self.reconnect_interval}s...")
                    await asyncio.sleep(self.reconnect_interval)
                    continue
                else:
                    break

            # Subscribe to assets
            if self._subscribed_assets:
                logger.info(f"Sending subscription for {len(self._subscribed_assets)} assets after connect")
                await self.subscribe(list(self._subscribed_assets))

            # Run message loop
            await self._run_loop()

            # Handle disconnect
            if self._on_disconnect:
                self._on_disconnect()

            if not self._running:
                break

            if auto_reconnect:
                logger.info(f"Reconnecting in {self.reconnect_interval}s...")
                await asyncio.sleep(self.reconnect_interval)
            else:
                break

    async def run_until_cancelled(self) -> None:
        """Run until cancelled or stopped."""
        try:
            await self.run(auto_reconnect=True)
        except asyncio.CancelledError:
            await self.disconnect()

    def stop(self) -> None:
        """Stop the WebSocket client."""
        self._running = False


class OrderbookManager:
    """
    High-level orderbook manager with WebSocket subscription.

    Provides a simpler interface for tracking multiple orderbooks
    with callbacks for price updates.

    Example:
        manager = OrderbookManager()

        @manager.on_price_update
        async def handle_price(asset_id: str, mid_price: float):
            print(f"{asset_id}: {mid_price}")

        await manager.start(["token_1", "token_2"])
    """

    def __init__(self):
        """Initialize orderbook manager."""
        self._ws = MarketWebSocket()
        self._price_callback: Optional[Callable[[str, float, float, float], None]] = None
        self._connected = False

        # Set up internal callbacks
        @self._ws.on_book
        async def on_book(snapshot: OrderbookSnapshot):
            if self._price_callback:
                try:
                    result = self._price_callback(
                        snapshot.asset_id,
                        snapshot.mid_price,
                        snapshot.best_bid,
                        snapshot.best_ask
                    )
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.error(f"Error in price callback: {e}")

        @self._ws.on_price_change
        async def on_price_change(market: str, changes: List[PriceChange]):
            if self._price_callback:
                for pc in changes:
                    if not pc.asset_id:
                        continue
                    snapshot = self._ws.get_orderbook(pc.asset_id)
                    if snapshot:
                        try:
                            result = self._price_callback(
                                snapshot.asset_id,
                                snapshot.mid_price,
                                snapshot.best_bid,
                                snapshot.best_ask
                            )
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception as e:
                            logger.error(f"Error in price callback: {e}")

        @self._ws.on_connect
        def on_connect():  # pyright: ignore[reportUnusedFunction]
            self._connected = True

        @self._ws.on_disconnect
        def on_disconnect():  # pyright: ignore[reportUnusedFunction]
            self._connected = False

    @property
    def is_connected(self) -> bool:
        """Check if connected."""
        return self._connected

    def get_price(self, asset_id: str) -> float:
        """Get current mid price for asset."""
        return self._ws.get_mid_price(asset_id)

    def get_orderbook(self, asset_id: str) -> Optional[OrderbookSnapshot]:
        """Get cached orderbook for asset."""
        return self._ws.get_orderbook(asset_id)

    def on_price_update(
        self,
        callback: Callable[[str, float, float, float], None]
    ) -> Callable[[str, float, float, float], None]:
        """
        Set callback for price updates.

        Callback receives: asset_id, mid_price, best_bid, best_ask
        """
        self._price_callback = callback
        return callback

    async def start(self, asset_ids: List[str]) -> None:
        """
        Start tracking orderbooks.

        Args:
            asset_ids: Token IDs to track
        """
        await self._ws.subscribe(asset_ids)
        await self._ws.run(auto_reconnect=True)

    async def subscribe(self, asset_ids: List[str]) -> bool:
        """Subscribe to additional assets."""
        return await self._ws.subscribe_more(asset_ids)

    async def unsubscribe(self, asset_ids: List[str]) -> bool:
        """Unsubscribe from assets."""
        return await self._ws.unsubscribe(asset_ids)

    def stop(self) -> None:
        """Stop the manager."""
        self._ws.stop()

    async def close(self) -> None:
        """Close connection."""
        await self._ws.disconnect()


class UserWebSocket:
    """
    WebSocket client for Polymarket user channel (orders and trades).
    """

    def __init__(
        self,
        api_key: str,
        secret: str,
        passphrase: str,
        url: str = WSS_USER_URL,
        reconnect_interval: float = 5.0,
        ping_interval: float = 20.0,
        ping_timeout: float = 10.0,
    ):
        self.url = url
        self.api_key = api_key
        self.secret = secret
        self.passphrase = passphrase
        self.reconnect_interval = reconnect_interval
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout

        self._ws_connect, self._connection_closed = _load_websockets()

        self._ws: Optional["WebSocketClientProtocol"] = None
        self._running = False
        self._subscribed_markets: Set[str] = set()

        self._on_trade: Optional[UserTradeCallback] = None
        self._on_order: Optional[UserOrderCallback] = None
        self._on_error: Optional[ErrorCallback] = None
        self._on_connect: Optional[Callable[[], None]] = None
        self._on_disconnect: Optional[Callable[[], None]] = None

    @property
    def is_connected(self) -> bool:
        if self._ws is None:
            return False
        try:
            from websockets.protocol import State
            return self._ws.state == State.OPEN
        except (ImportError, AttributeError):
            try:
                return self._ws.open
            except AttributeError:
                return False

    def on_trade(self, callback: UserTradeCallback) -> UserTradeCallback:
        self._on_trade = callback
        return callback

    def on_order(self, callback: UserOrderCallback) -> UserOrderCallback:
        self._on_order = callback
        return callback

    def on_error(self, callback: ErrorCallback) -> ErrorCallback:
        self._on_error = callback
        return callback

    def on_connect(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._on_connect = callback
        return callback

    def on_disconnect(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._on_disconnect = callback
        return callback

    async def connect(self) -> bool:
        try:
            if self._ws_connect is None:
                raise RuntimeError("websockets is not installed")

            self._ws = await self._ws_connect(
                self.url,
                ping_interval=self.ping_interval,
                ping_timeout=self.ping_timeout,
            )
            logger.info(f"User WebSocket connected to {self.url}")
            if self._on_connect:
                self._on_connect()
            return True
        except Exception as e:
            logger.error(f"User WebSocket connection failed: {e}")
            if self._on_error:
                self._on_error(e)
            return False

    async def disconnect(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
            logger.info("User WebSocket disconnected")
            if self._on_disconnect:
                self._on_disconnect()

    async def subscribe(self, markets: Optional[List[str]] = None) -> bool:
        """
        Subscribe to user channel. If markets are provided, limits to those markets.
        Note: markets is an array of condition_ids.
        """
        if markets is not None:
            self._subscribed_markets.update(markets)

        if not self.is_connected:
            return True

        subscribe_msg = {
            "auth": {
                "apiKey": self.api_key,
                "secret": self.secret,
                "passphrase": self.passphrase
            },
            "type": "user"
        }
        
        # In Polymarket WS user channel, markets might be required or optional.
        # It's an array of condition_ids. If empty, it might still work for all.
        if self._subscribed_markets:
            subscribe_msg["markets"] = list(self._subscribed_markets)

        try:
            msg_json = json.dumps(subscribe_msg)
            await self._ws.send(msg_json)
            logger.info(f"Subscribed to User Channel successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to subscribe to User Channel: {e}")
            if self._on_error:
                self._on_error(e)
            return False

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        event_type = data.get("event_type")
        if not event_type:
            # Could be a heartbeat or ack message
            return

        if event_type == "trade":
            trade = TradeEvent.from_message(data)
            if self._on_trade:
                try:
                    result = self._on_trade(trade)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.error(f"Error in UserTrade callback: {e}")
        elif event_type == "order":
            order = OrderEvent.from_message(data)
            if self._on_order:
                try:
                    result = self._on_order(order)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.error(f"Error in UserOrder callback: {e}")
        else:
            logger.debug(f"Unknown user event type: {event_type}")

    async def _run_loop(self) -> None:
        while self._running and self.is_connected:
            try:
                message = await asyncio.wait_for(
                    self._ws.recv(),
                    timeout=self.ping_interval + 5
                )
                data = json.loads(message)

                if isinstance(data, list):
                    for item in data:
                        await self._handle_message(item)
                else:
                    await self._handle_message(data)

            except asyncio.TimeoutError:
                pass
            except self._connection_closed:
                break
            except json.JSONDecodeError:
                pass
            except Exception as e:
                logger.error(f"User WS error processing message: {e}")
                if self._on_error:
                    self._on_error(e)

    async def run(self, auto_reconnect: bool = True) -> None:
        self._running = True

        while self._running:
            if not await self.connect():
                if auto_reconnect:
                    await asyncio.sleep(self.reconnect_interval)
                    continue
                else:
                    break

            # Always re-subscribe on connect
            await self.subscribe()

            await self._run_loop()

            if self._on_disconnect:
                self._on_disconnect()

            if not self._running:
                break

            if auto_reconnect:
                await asyncio.sleep(self.reconnect_interval)
            else:
                break

    def stop(self) -> None:
        self._running = False
