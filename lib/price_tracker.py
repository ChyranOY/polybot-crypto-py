"""
Price Tracker - Price History and Flash Crash Detection

Provides:
- Price history storage with timestamps
- Flash crash detection (absolute probability drops)
- Price point data structures
- Configurable lookback windows

Usage:
    from lib import PriceTracker, FlashDropEvent

    tracker = PriceTracker(lookback_seconds=10, drop_threshold=0.30)

    # Record prices
    tracker.record("up", 0.55)
    tracker.record("down", 0.45)

    # Check for flash crash
    event = tracker.detect_flash_drop()
    if event:
        print(f"Crash on {event.side}: {event.old_price} -> {event.new_price}")
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Dict, Deque, List


@dataclass
class PricePoint:
    """A price observation at a specific time."""

    timestamp: float
    price: float
    side: str  # "up" or "down"


@dataclass
class FlashDropEvent:
    """Detected flash crash event."""

    side: str  # "up" or "down"
    old_price: float
    new_price: float
    drop: float  # Absolute drop amount
    timestamp: float

    @property
    def drop_percent(self) -> float:
        """Calculate percentage drop."""
        if self.old_price > 0:
            return (self.old_price - self.new_price) / self.old_price * 100
        return 0.0

@dataclass
class PriceTracker:
    """
    Tracks price history and detects flash crashes.

    A flash crash is when the probability drops by more than the threshold
    within the lookback window (e.g., 0.30 means price drops from 0.5 to 0.2).
    """

    lookback_seconds: int = 10
    drop_threshold: float = 0.30

    # Current price per side
    _current_price: Dict[str, float] = field(default_factory=dict)
    
    # Monotonic decreasing queues to track max prices efficiently (for drop detection)
    _max_q: Dict[str, Deque[tuple[float, float]]] = field(default_factory=dict)

    def __post_init__(self):
        """Initialize data structures."""
        self._current_price = {
            "up": 0.0,
            "down": 0.0,
        }
        self._max_q = {
            "up": deque(),
            "down": deque(),
        }

    def record(self, side: str, price: float, timestamp: Optional[float] = None) -> None:
        """
        Record a price point.

        Args:
            side: "up" or "down"
            price: Current price (0-1)
            timestamp: Optional timestamp (defaults to now)
        """
        if side not in self._max_q:
            return

        if price <= 0:
            return

        ts = timestamp if timestamp is not None else time.time()
        self._current_price[side] = price
        
        # Keep only the prices within the lookback_seconds window
        target_time = ts - self.lookback_seconds
        
        # Maintain max monotonic queue (for drops)
        max_q = self._max_q[side]
        while max_q and max_q[-1][1] <= price:
            max_q.pop()
        max_q.append((ts, price))
        
        # Remove old elements from max monotonic queue, but ALWAYS keep at least one historical point
        # to correctly calculate drops against sparse updates
        while max_q and max_q[0][0] < target_time:
            max_q.popleft()

    def record_prices(self, prices: Dict[str, float]) -> None:
        """
        Record multiple prices at once.

        Args:
            prices: Dictionary of {side: price}
        """
        now = time.time()
        for side, price in prices.items():
            self.record(side, price, now)

    def get_history_count(self, side: str) -> int:
        """Get number of recorded prices for a side in current lookback window (max_q_len)."""
        max_len = 0
        if side in self._max_q:
            max_len = len(self._max_q[side])
        return max_len

    def get_current_price(self, side: str) -> float:
        """Get most recent price for a side."""
        if side in self._current_price:
            return self._current_price[side]
        return 0.0

    def detect_flash_drop(self, side: Optional[str] = None) -> Optional[FlashDropEvent]:
        """
        Detect if a flash crash occurred.

        Args:
            side: Specific side to check, or None to check both

        Returns:
            FlashDropEvent if crash detected, None otherwise
        """
        sides_to_check = [side] if side else ["up", "down"]
        now = time.time()

        for s in sides_to_check:
            if s not in self._max_q:
                continue

            max_q = self._max_q[s]
            if len(max_q) < 1:
                continue

            # Get current price
            current_price = self._current_price[s]

            # Find the maximum price within the lookback window using monotonic queue
            max_price = max_q[0][1]

            # Calculate absolute drop
            drop = max_price - current_price

            if drop >= self.drop_threshold:
                return FlashDropEvent(
                    side=s,
                    old_price=max_price,
                    new_price=current_price,
                    drop=drop,
                    timestamp=now,
                )

        return None

    def clear(self, side: Optional[str] = None) -> None:
        """
        Clear price history.

        Args:
            side: Specific side to clear, or None to clear all
        """
        if side:
            if side in self._max_q:
                self._max_q[side].clear()
            if side in self._current_price:
                self._current_price[side] = 0.0
        else:
            for s in self._max_q:
                self._max_q[s].clear()
                self._current_price[s] = 0.0
