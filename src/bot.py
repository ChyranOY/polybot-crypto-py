"""
Polymarket Arbitrage Bot - Main Trading Interface

The core TradingBot class provides a high-level interface for interacting
with the Polymarket decentralized prediction market platform. This module
handles order placement, cancellation, position management, and real-time
market data retrieval.

Features:
    - Gasless transactions via Builder Program integration
    - Encrypted private key storage support
    - Modular strategy support for custom trading logic
    - Comprehensive order management (place, cancel, query)
    - Real-time market data and orderbook access

Example:
    from src.bot import TradingBot, Config

    # Initialize with config file
    bot = TradingBot(config_path="config.yaml")

    # Or initialize manually
    from src.config import BuilderConfig
    config = Config(
        safe_address="0x...",
        builder=BuilderConfig(relayer_api_key="...", relayer_api_key_address="...")
    )
    bot = TradingBot(config=config, private_key="0x...")

    # Or use encrypted key file
    bot = TradingBot(
        config=config,
        encrypted_key_path="credentials/key.enc",
        password="your_password"
    )

    # Place a limit order
    result = await bot.place_order(
        token_id="123...",
        price=0.65,
        size=10.0,
        side="BUY"
    )

    if result.success:
        print(f"Order placed: {result.order_id}")
    else:
        print(f"Order failed: {result.message}")

Note:
    All methods are async and must be awaited. The bot automatically
    handles initialization, signing, and API communication.
"""

import os
import json
import asyncio
import logging
from typing import Optional, Dict, Any, List, Callable, TypeVar
from dataclasses import dataclass, field
from enum import Enum

from .config import Config, BuilderConfig
from .client import RelayerClient
from .crypto import KeyManager, CryptoError, InvalidPasswordError

try:
    from py_clob_client_v2.client import ClobClient as OfficialClobClient
    from py_clob_client_v2.clob_types import OrderArgs, OrderType as SdkOrderType, ApiCreds, OpenOrderParams, TradeParams
    from py_clob_client_v2.order_builder.constants import BUY, SELL
except Exception:
    OfficialClobClient = None
    OrderArgs = None
    SdkOrderType = None
    ApiCreds = None
    OpenOrderParams = None
    TradeParams = None
    BUY = "BUY"
    SELL = "SELL"


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

T = TypeVar("T")

class OrderSide(str, Enum):
    """Order side constants."""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """Order type constants."""
    GTC = "GTC"  # Good Till Cancelled
    GTD = "GTD"  # Good Till Date
    FOK = "FOK"  # Fill Or Kill


@dataclass
class OrderResult:
    """Result of an order operation."""
    success: bool
    order_id: Optional[str] = None
    status: Optional[str] = None
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(cls, response: Any) -> "OrderResult":
        if response is None:
            return cls(success=False, message="Empty response", data={})

        if not isinstance(response, dict):
            if hasattr(response, "__dict__"):
                response = dict(response.__dict__)
            else:
                return cls(
                    success=True,
                    order_id=str(response),
                    message="Order placed successfully",
                    data={"response": response},
                )

        success = bool(response.get("success", False))
        status = response.get("status") or response.get("order_status")

        nested = response.get("data") if isinstance(response.get("data"), dict) else {}
        order_id = (
            response.get("orderId")
            or response.get("orderID")
            or response.get("order_id")
            or response.get("id")
            or nested.get("orderId")
            or nested.get("orderID")
            or nested.get("order_id")
            or nested.get("id")
        )

        error_msg = (
            response.get("errorMsg")
            or response.get("error_message")
            or response.get("error")
            or response.get("message")
            or ""
        )

        if not success and (order_id or status):
            success = True

        message = error_msg if not success else "Order placed successfully"

        return cls(
            success=success,
            order_id=order_id,
            status=status,
            message=message,
            data=response,
        )


class TradingBotError(Exception):
    """Base exception for trading bot errors."""
    pass


class NotInitializedError(TradingBotError):
    """Raised when bot is not initialized."""
    pass


class TradingBot:
    """
    Main trading bot class for Polymarket.

    Provides a high-level interface for:
    - Order placement and cancellation
    - Position management
    - Trade history
    - Gasless transactions (with Builder Program)

    Attributes:
        config: Bot configuration
        client: CLOB API client (official SDK)
        relayer_client: Relayer API client (if gasless enabled)
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        config: Optional[Config] = None,
        safe_address: Optional[str] = None,
        builder_creds: Optional[BuilderConfig] = None,
        private_key: Optional[str] = None,
        encrypted_key_path: Optional[str] = None,
        password: Optional[str] = None,
        log_level: int = logging.INFO
    ):
        """
        Initialize trading bot.

        Can be initialized in multiple ways:

        1. From config file:
           bot = TradingBot(config_path="config.yaml")

        2. From Config object:
           bot = TradingBot(config=my_config)

        3. With manual parameters:
           bot = TradingBot(
               safe_address="0x...",
               builder_creds=builder_creds,
               private_key="0x..."
           )

        4. With encrypted key:
           bot = TradingBot(
               safe_address="0x...",
               encrypted_key_path="credentials/key.enc",
               password="mypassword"
           )

        Args:
            config_path: Path to config YAML file
            config: Config object
            safe_address: Safe/Proxy wallet address
            builder_creds: Builder Program credentials
            private_key: Raw private key (with 0x prefix)
            encrypted_key_path: Path to encrypted key file
            password: Password for encrypted key
            log_level: Logging level
        """
        # Set log level
        logger.setLevel(log_level)

        # Load configuration
        if config_path:
            self.config = Config.load(config_path)
        elif config:
            self.config = config
        else:
            self.config = Config()

        # Override with provided parameters
        if safe_address:
            self.config.safe_address = safe_address
        if builder_creds:
            self.config.builder = builder_creds
            self.config.use_gasless = True

        self._private_key: Optional[str] = None

        self.client: Optional[Any] = None
        self.relayer_client: Optional[RelayerClient] = None
        self._api_creds: Optional[Any] = None

        if private_key:
            self._private_key = private_key if private_key.startswith("0x") else "0x" + private_key
        elif encrypted_key_path and password:
            self._private_key = self._load_encrypted_key(encrypted_key_path, password)

        # Initialize API clients
        self._init_clients()

        logger.info(f"TradingBot initialized (gasless: {self.config.use_gasless})")

    def _load_encrypted_key(self, filepath: str, password: str) -> str:
        try:
            manager = KeyManager()
            private_key = manager.load_and_decrypt(password, filepath)
            logger.info(f"Loaded encrypted key from {filepath}")
            return private_key
        except FileNotFoundError:
            raise TradingBotError(f"Encrypted key file not found: {filepath}")
        except InvalidPasswordError:
            raise TradingBotError("Invalid password for encrypted key")
        except CryptoError as e:
            raise TradingBotError(f"Failed to load encrypted key: {e}")

    def _init_clients(self) -> None:
        """Initialize API clients."""
        if self._private_key:
            self._init_client()

        # Relayer client (for gasless)
        if self.config.use_gasless:
            self.relayer_client = RelayerClient(
                host=self.config.relayer.host,
                chain_id=self.config.clob.chain_id,
                builder_creds=self.config.builder,
                tx_type=self.config.relayer.tx_type,
            )
            logger.info("Relayer client initialized (gasless enabled)")

    def _init_client(self) -> None:
        try:
            if OfficialClobClient is None:
                raise TradingBotError("py-clob-client-v2 is not installed")

            host = self.config.clob.host or "https://clob.polymarket.com"
            chain_id = self.config.clob.chain_id or 137
            signature_type = self.config.clob.signature_type or 2
            funder = self.config.safe_address

            # In V2, builder_config is just a simple string (the builderCode), not an HMAC config object.
            # You can also pass it in the constructor or per order.
            builder_config = None
            if self.config.use_gasless and self.config.builder and self.config.builder.builder_code:
                # Use builder code directly
                pass

            self.client = OfficialClobClient(
                host=host,
                key=self._private_key,
                chain_id=chain_id,
                signature_type=signature_type,
                funder=funder,
            )

            creds = self.client.create_or_derive_api_key()
            if creds:
                self.client.set_api_creds(creds)
                self._api_creds = creds
        except Exception as e:
            logger.error(f"Failed to initialize client: {e}")
            self.client = None

    async def _run_in_thread(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run a blocking call in a worker thread to avoid event loop stalls."""
        return await asyncio.to_thread(func, *args, **kwargs)

    def is_initialized(self) -> bool:
        """Check if bot is properly initialized."""
        return self.client is not None and bool(self.config.safe_address)

    @property
    def api_creds(self) -> Optional[Any]:
        """Get the API credentials used by the client."""
        return self._api_creds

    def require_client(self) -> Any:
        if not self.client:
            raise NotInitializedError("Client not initialized. Provide private_key or encrypted_key.")
        return self.client

    async def place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        order_type: str = "GTC",
        fee_rate_bps: int = 0
    ) -> OrderResult:
        """
        Place a limit order.

        Args:
            token_id: Market token ID
            price: Price per share (0-1)
            size: Number of shares
            side: 'BUY' or 'SELL'
            order_type: Order type (GTC, GTD, FOK)
            fee_rate_bps: Fee rate in basis points

        Returns:
            OrderResult with order status
        """
        client = self.require_client()

        try:
            sdk_side = BUY if side.upper() == "BUY" else SELL
            sdk_order_type = getattr(SdkOrderType, order_type.upper(), SdkOrderType.GTC)

            order_args_kwargs = {
                "token_id": token_id,
                "price": price,
                "size": size,
                "side": sdk_side,
            }
            
            # 添加 builder_code 属性，确保订单归因和免 Gas 费奖励生效
            if self.config.builder and self.config.builder.builder_code:
                order_args_kwargs["builder_code"] = self.config.builder.builder_code

            order_args = OrderArgs(**order_args_kwargs)

            signed_order = await self._run_in_thread(client.create_order, order_args)
            # 明确设置 post_only=False，移除/禁用 Post-Only 保护
            response = await self._run_in_thread(client.post_order, signed_order, sdk_order_type, post_only=False)

            logger.info(
                f"Order placed: {side} {size}@{price} "
                f"(token: {token_id[:16]}...)"
            )

            return OrderResult.from_response(response)

        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            return OrderResult(
                success=False,
                message=str(e)
            )

    async def place_orders(
        self,
        orders: List[Dict[str, Any]],
        order_type: str = "GTC"
    ) -> List[OrderResult]:
        """
        Place multiple orders.

        Args:
            orders: List of order dictionaries with keys:
                - token_id: Market token ID
                - price: Price per share
                - size: Number of shares
                - side: 'BUY' or 'SELL'
            order_type: Order type (GTC, GTD, FOK)

        Returns:
            List of OrderResults
        """
        results = []
        for order_data in orders:
            result = await self.place_order(
                token_id=order_data["token_id"],
                price=order_data["price"],
                size=order_data["size"],
                side=order_data["side"],
                order_type=order_type,
            )
            results.append(result)

            # Small delay between orders to avoid rate limits
            await asyncio.sleep(0.1)

        return results

    async def cancel_order(self, order_id: str) -> OrderResult:
        """
        Cancel a specific order.

        Args:
            order_id: Order ID to cancel

        Returns:
            OrderResult with cancellation status
        """
        try:
            client = self.require_client()
            response = await self._run_in_thread(client.cancel, order_id)
            logger.info(f"Order cancelled: {order_id}")
            return OrderResult(
                success=True,
                order_id=order_id,
                message="Order cancelled",
                data=response
            )
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return OrderResult(
                success=False,
                order_id=order_id,
                message=str(e)
            )

    async def cancel_all_orders(self) -> OrderResult:
        """
        Cancel all open orders.

        Returns:
            OrderResult with cancellation status
        """
        try:
            client = self.require_client()
            response = await self._run_in_thread(client.cancel_all)
            logger.info("All orders cancelled")
            return OrderResult(
                success=True,
                message="All orders cancelled",
                data=response
            )
        except Exception as e:
            logger.error(f"Failed to cancel orders: {e}")
            return OrderResult(success=False, message=str(e))

    async def cancel_market_orders(
        self,
        market: Optional[str] = None,
        asset_id: Optional[str] = None
    ) -> OrderResult:
        """
        Cancel orders for a specific market.

        Args:
            market: Condition ID of the market (optional)
            asset_id: Token/asset ID (optional)

        Returns:
            OrderResult with cancellation status
        """
        try:
            client = self.require_client()
            response = await self._run_in_thread(
                client.cancel_market_orders,
                market=market,
                asset_id=asset_id,
            )
            logger.info(f"Market orders cancelled (market: {market or 'all'}, asset: {asset_id or 'all'})")
            return OrderResult(
                success=True,
                message=f"Orders cancelled for market {market or 'all'}",
                data=response
            )
        except Exception as e:
            logger.error(f"Failed to cancel market orders: {e}")
            return OrderResult(success=False, message=str(e))

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        Get all open orders.

        Returns:
            List of open orders
        """
        try:
            client = self.require_client()
            orders = await self._run_in_thread(client.get_open_orders, OpenOrderParams())
            logger.debug(f"Retrieved {len(orders)} open orders")
            return orders
        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            return []

    async def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """
        Get order details.

        Args:
            order_id: Order ID

        Returns:
            Order details or None
        """
        try:
            client = self.require_client()
            return await self._run_in_thread(client.get_order, order_id)
        except Exception as e:
            logger.error(f"Failed to get order {order_id}: {e}")
            return None

    async def get_trades(
        self,
        token_id: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get trade history.

        Args:
            token_id: Optional token ID to filter
            limit: Maximum number of trades

        Returns:
            List of trades
        """
        try:
            client = self.require_client()
            params = TradeParams()
            if token_id:
                params.asset_id = token_id
            trades = await self._run_in_thread(client.get_trades, params)
            logger.debug(f"Retrieved {len(trades) if trades else 0} trades")
            return trades if trades else []
        except Exception as e:
            logger.error(f"Failed to get trades: {e}")
            return []

    async def get_order_book(self, token_id: str) -> Dict[str, Any]:
        """
        Get order book for a token.

        Args:
            token_id: Market token ID

        Returns:
            Order book data
        """
        try:
            client = self.require_client()
            order_book = await self._run_in_thread(client.get_order_book, token_id)
            if hasattr(order_book, "__dict__"):
                return dict(order_book.__dict__)
            return order_book if isinstance(order_book, dict) else {}
        except Exception as e:
            logger.error(f"Failed to get order book: {e}")
            return {}

    async def get_market_price(self, token_id: str) -> Dict[str, Any]:
        """
        Get current market price for a token.

        Args:
            token_id: Market token ID

        Returns:
            Price data
        """
        try:
            client = self.require_client()
            mid = await self._run_in_thread(client.get_midpoint, token_id)
            return {"mid": mid, "price": mid}
        except Exception as e:
            logger.error(f"Failed to get market price: {e}")
            return {}

    async def deploy_safe_if_needed(self) -> bool:
        """
        Deploy Safe proxy wallet if not already deployed.

        Returns:
            True if deployment was needed or successful
        """
        if not self.config.use_gasless or not self.relayer_client:
            logger.debug("Gasless not enabled, skipping Safe deployment")
            return False

        try:
            response = await self._run_in_thread(
                self.relayer_client.deploy_safe,
                self.config.safe_address,
            )
            logger.info(f"Safe deployment initiated: {response}")
            return True
        except Exception as e:
            logger.warning(f"Safe deployment failed (may already be deployed): {e}")
            return False

    def create_order_dict(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str
    ) -> Dict[str, Any]:
        """
        Create an order dictionary for batch processing.

        Args:
            token_id: Market token ID
            price: Price per share
            size: Number of shares
            side: 'BUY' or 'SELL'

        Returns:
            Order dictionary
        """
        return {
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": side.upper(),
        }


# Convenience function for quick initialization
def create_bot(
    config_path: str = "config.yaml",
    private_key: Optional[str] = None,
    encrypted_key_path: Optional[str] = None,
    password: Optional[str] = None,
    **kwargs
) -> TradingBot:
    """
    Create a TradingBot instance with common options.

    Args:
        config_path: Path to config file
        private_key: Private key (with 0x prefix)
        encrypted_key_path: Path to encrypted key file
        password: Password for encrypted key
        **kwargs: Additional arguments for TradingBot

    Returns:
        Configured TradingBot instance
    """
    return TradingBot(
        config_path=config_path,
        private_key=private_key,
        encrypted_key_path=encrypted_key_path,
        password=password,
        **kwargs
    )
