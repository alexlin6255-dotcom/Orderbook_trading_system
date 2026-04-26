"""
Massive.io Market Data → OrderBook Integration
===============================================
Connects live U.S. equity data (trades + NBBO quotes) from Massive.com
(formerly Polygon.io) into the orderbook engine, position tracker,
market stats, and adaptive strategy layer.

Usage:
    python massive_integration.py --tickers AAPL MSFT --mode live
    python massive_integration.py --tickers AAPL      --mode delayed   (free tier)

Install deps:
    pip install massive websockets asyncio

API key is loaded from the MASSIVE_API_KEY environment variable.
"""

from datetime import datetime
from collections import deque
from typing import Optional
from dataclasses import dataclass, field
from enum import Enum
from datetime import date, timedelta
import threading
from massive import RESTClient
from typing import List
from massive.websocket.models import WebSocketMessage
from massive import WebSocketClient
import os
import asyncio
import json
import time
import math
import uuid
import random
import bisect
import argparse
import logging
import warnings
import urllib3
warnings.filterwarnings(
    "ignore", category=urllib3.exceptions.NotOpenSSLWarning)
# ── Logging — must be defined before anything else uses it ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("trading")
logging.getLogger("massive").setLevel(logging.ERROR)

# ══════════════════════════════════════════════════════════════════════════
#  ENUMS  (unchanged from core engine)
# ══════════════════════════════════════════════════════════════════════════


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    IOC = "IOC"
    FOK = "FOK"
    GTC = "GTC"


class OrderStatus(Enum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    PENDING = "PENDING"


class Regime(Enum):
    TRENDING = "TRENDING"
    MEAN_REVERTING = "MEAN_REVERTING"
    HIGH_VOL = "HIGH_VOL"
    UNKNOWN = "UNKNOWN"


# ══════════════════════════════════════════════════════════════════════════
#  CORE DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Order:
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    price: Optional[float] = None
    stop_price: Optional[float] = None
    order_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    filled_qty: float = 0.0
    status: OrderStatus = OrderStatus.OPEN
    timestamp: float = field(default_factory=time.time)

    @property
    def remaining_qty(self): return self.quantity - self.filled_qty

    @property
    def is_active(self):
        return self.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)


@dataclass
class Trade:
    symbol: str
    buy_order_id: str
    sell_order_id: str
    price: float
    quantity: float
    aggressor_side: OrderSide = OrderSide.BUY
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: float = field(default_factory=time.time)


# ══════════════════════════════════════════════════════════════════════════
#  MINIMAL SORTED DICT  (no external deps)
# ══════════════════════════════════════════════════════════════════════════

class SortedDict:
    def __init__(self):
        self._keys: list = []
        self._data: dict = {}

    def __setitem__(self, k, v):
        if k not in self._data:
            bisect.insort(self._keys, k)
        self._data[k] = v

    def __getitem__(self, k): return self._data[k]

    def __delitem__(self, k):
        idx = bisect.bisect_left(self._keys, k)
        if idx < len(self._keys) and self._keys[idx] == k:
            self._keys.pop(idx)
        del self._data[k]

    def __contains__(self, k): return k in self._data
    def __iter__(self): return iter(self._keys)
    def __bool__(self): return bool(self._data)
    def keys(self): return iter(self._keys)
    def items(self): return ((k, self._data[k]) for k in self._keys)
    def get(self, k, d=None): return self._data.get(k, d)


# ══════════════════════════════════════════════════════════════════════════
#  POSITION TRACKER
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Position:
    symbol: str
    qty: float = 0.0
    avg_cost: float = 0.0
    realised_pnl: float = 0.0
    trade_count: int = 0

    @property
    def is_flat(self): return abs(self.qty) < 1e-9
    @property
    def is_long(self): return self.qty > 1e-9

    def on_fill(self, fill_price: float, fill_qty: float, side: OrderSide):
        self.trade_count += 1
        if side == OrderSide.BUY:
            total = self.avg_cost * self.qty + fill_price * fill_qty
            self.qty += fill_qty
            self.avg_cost = total / self.qty if self.qty > 0 else 0
        else:
            if not self.is_flat:
                self.realised_pnl += (fill_price - self.avg_cost) * fill_qty
            self.qty -= fill_qty
            if self.is_flat:
                self.avg_cost = 0.0

    def unrealised_pnl(self, price: float) -> float:
        return 0.0 if self.is_flat else (price - self.avg_cost) * self.qty

    def total_pnl(self, price: float) -> float:
        return self.realised_pnl + self.unrealised_pnl(price)


# ══════════════════════════════════════════════════════════════════════════
#  MARKET STATISTICS  (driven by live Massive trade ticks)
# ══════════════════════════════════════════════════════════════════════════

class MarketStats:
    """
    Updated on every incoming trade tick from Massive.
    High/Low = running extremes of actual trade prints.
    VWAP     = rolling Σ(price×qty) / Σ(qty).
    Delta    = buy_volume − sell_volume (net aggressor pressure).
    Volatility = annualised std of log-returns over the rolling window.
    """

    def __init__(self, window: int = 200):
        self.window = window
        self.high = float('-inf')
        self.low = float('inf')
        self.buy_volume = 0.0
        self.sell_volume = 0.0
        self._prices = deque(maxlen=window)
        self._volumes = deque(maxlen=window)
        self._notional = deque(maxlen=window)
        self._returns = deque(maxlen=window)

    def on_trade(self, price: float, qty: float, side: OrderSide):
        if price > self.high:
            self.high = price
        if price < self.low:
            self.low = price
        if side == OrderSide.BUY:
            self.buy_volume += qty
        else:
            self.sell_volume += qty
        if self._prices:
            prev = self._prices[-1]
            if prev > 0:
                self._returns.append(math.log(price / prev))
        self._prices.append(price)
        self._volumes.append(qty)
        self._notional.append(price * qty)

    @property
    def total_volume(self): return self.buy_volume + self.sell_volume
    @property
    def delta(self): return self.buy_volume - self.sell_volume

    @property
    def vwap(self) -> Optional[float]:
        tq = sum(self._volumes)
        return sum(self._notional) / tq if tq else None

    @property
    def volatility(self) -> float:
        if len(self._returns) < 5:
            return 0.0
        mean = sum(self._returns) / len(self._returns)
        var = sum((r - mean)**2 for r in self._returns) / len(self._returns)
        return math.sqrt(var) * math.sqrt(252 * 6.5 * 3600)  # annualised

    @property
    def momentum(self) -> float:
        if len(self._prices) < 2:
            return 0.0
        return self._prices[-1] - self._prices[0]

    def moving_average(self, n: int) -> Optional[float]:
        if len(self._prices) < n:
            return None
        return sum(list(self._prices)[-n:]) / n

    @property
    def last_price(self) -> Optional[float]:
        return self._prices[-1] if self._prices else None


# ══════════════════════════════════════════════════════════════════════════
#  NBBO QUOTE TRACKER
#  Directly populated from Massive "Q" (quote) events.
#  Gives us real best bid / best ask without simulating the book.
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class NBBOQuote:
    """
    National Best Bid and Offer — the tightest spread available
    across all US exchanges, consolidated by the SIP.

    Fields map directly to Massive WebSocket "Q" event fields:
        bp  → bid_price       (best bid across all exchanges)
        bs  → bid_size        (round lots at best bid)
        ap  → ask_price       (best ask across all exchanges)
        as_ → ask_size        (round lots at best ask)
        t   → timestamp_ms    (SIP timestamp, Unix ms)
    """
    symbol: str = ""
    bid_price: float = 0.0
    bid_size: float = 0.0
    ask_price: float = 0.0
    ask_size: float = 0.0
    timestamp_ms: int = 0

    @property
    def mid_price(self) -> Optional[float]:
        if self.bid_price > 0 and self.ask_price > 0:
            return (self.bid_price + self.ask_price) / 2
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.bid_price > 0 and self.ask_price > 0:
            return self.ask_price - self.bid_price
        return None

    @property
    def spread_bps(self) -> Optional[float]:
        """Spread in basis points — standardised across price levels."""
        m = self.mid_price
        s = self.spread
        if m and s and m > 0:
            return (s / m) * 10_000
        return None

    def update_from_massive(self, msg: dict):
        """
        Parses a Massive WebSocket quote event (ev="Q").

        Massive quote message fields:
          ev  : "Q"           event type
          sym : "AAPL"        ticker
          bp  : 182.50        best bid price
          bs  : 3             best bid size (round lots × 100 shares)
          ap  : 182.51        best ask price
          as  : 5             best ask size
          t   : 1700000000000 SIP timestamp (Unix ms)
        """
        self.symbol = msg.get("sym", self.symbol)
        self.bid_price = float(msg.get("bp", 0) or 0)
        self.bid_size = float(msg.get("bs", 0) or 0) * 100   # lots → shares
        self.ask_price = float(msg.get("ap", 0) or 0)
        self.ask_size = float(msg.get("as", 0) or 0) * 100
        self.timestamp_ms = int(msg.get("t",  0) or 0)


# ══════════════════════════════════════════════════════════════════════════
#  ORDERBOOK  (price-time priority matching engine)
# ══════════════════════════════════════════════════════════════════════════

class OrderBook:
    """
    Internal limit orderbook.

    In live trading this is used for:
      - Simulating strategy order fills against synthetic liquidity
        seeded from the NBBO spread
      - Tracking all submitted strategy orders and their lifecycle
      - Generating Trade records for position accounting

    The NBBO (from Massive quotes) is the *market* book — we don't
    replicate the full exchange book here, just our own orders.
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        self._bids = SortedDict()
        self._asks = SortedDict()
        self._stop_orders = []
        self._orders = {}
        self._trade_log = []

    # ── Accessors ──────────────────────────────────────────────────────────
    def best_bid(self) -> Optional[float]:
        if not self._bids:
            return None
        return -next(iter(self._bids))

    def best_ask(self) -> Optional[float]:
        if not self._asks:
            return None
        return next(iter(self._asks))

    def mid_price(self) -> Optional[float]:
        b, a = self.best_bid(), self.best_ask()
        return (b + a) / 2 if b and a else None

    def spread(self) -> Optional[float]:
        b, a = self.best_bid(), self.best_ask()
        return (a - b) if b and a else None

    def get_order(self, oid): return self._orders.get(oid)
    def get_trades(self): return list(self._trade_log)

    # ── Order submission ───────────────────────────────────────────────────
    def submit_order(self, order: Order) -> list:
        self._validate(order)
        self._orders[order.order_id] = order
        dispatch = {
            OrderType.MARKET: self._handle_market,
            OrderType.LIMIT: self._handle_limit,
            OrderType.STOP: self._handle_stop,
            OrderType.IOC: self._handle_ioc,
            OrderType.FOK: self._handle_fok,
            OrderType.GTC: self._handle_gtc,
        }
        trades = dispatch[order.order_type](order)
        self._trade_log.extend(trades)
        self._check_stop_triggers(trades)
        return trades

    def cancel_order(self, oid: str) -> bool:
        order = self._orders.get(oid)
        if not order or not order.is_active:
            return False
        if order.status == OrderStatus.PENDING:
            self._stop_orders = [
                o for o in self._stop_orders if o.order_id != oid]
            order.status = OrderStatus.CANCELLED
            return True
        self._remove_from_book(order)
        order.status = OrderStatus.CANCELLED
        return True

    # ── Handlers ───────────────────────────────────────────────────────────
    def _handle_market(self, o): return self._match(o, None)

    def _handle_limit(self, o):
        trades = self._match(o, o.price)
        if o.is_active:
            self._add_to_book(o)
        return trades

    def _handle_gtc(self, o): return self._handle_limit(o)

    def _handle_stop(self, o):
        o.status = OrderStatus.PENDING
        self._stop_orders.append(o)
        return []

    def _handle_ioc(self, o):
        trades = self._match(o, o.price)
        if o.is_active:
            o.status = OrderStatus.CANCELLED
        return trades

    def _handle_fok(self, o):
        if not self._can_fill_fully(o):
            o.status = OrderStatus.CANCELLED
            return []
        return self._match(o, o.price)

    # ── Matching engine ────────────────────────────────────────────────────
    def _match(self, aggressor: Order, price_limit: Optional[float]) -> list:
        trades = []
        book_side = self._asks if aggressor.side == OrderSide.BUY else self._bids
        for price_key in list(book_side.keys()):
            if aggressor.remaining_qty <= 0:
                break
            rp = -price_key if aggressor.side == OrderSide.SELL else price_key
            if rp <= 0:
                continue
            if price_limit is not None:
                if aggressor.side == OrderSide.BUY and rp > price_limit:
                    break
                if aggressor.side == OrderSide.SELL and rp < price_limit:
                    break
            queue = book_side[price_key]
            while queue and aggressor.remaining_qty > 0:
                resting = queue[0]
                fill_qty = min(aggressor.remaining_qty, resting.remaining_qty)
                trade = Trade(
                    symbol=self.symbol,
                    buy_order_id=aggressor.order_id if aggressor.side == OrderSide.BUY else resting.order_id,
                    sell_order_id=aggressor.order_id if aggressor.side == OrderSide.SELL else resting.order_id,
                    price=rp,
                    quantity=fill_qty,
                    aggressor_side=aggressor.side,
                )
                trades.append(trade)
                aggressor.filled_qty += fill_qty
                resting.filled_qty += fill_qty
                aggressor.status = OrderStatus.FILLED if aggressor.remaining_qty == 0 else OrderStatus.PARTIALLY_FILLED
                resting.status = OrderStatus.FILLED if resting.remaining_qty == 0 else OrderStatus.PARTIALLY_FILLED
                if resting.remaining_qty == 0:
                    queue.pop(0)
            if not queue:
                del book_side[price_key]
        return trades

    def _check_stop_triggers(self, recent_trades):
        if not recent_trades or not self._stop_orders:
            return
        last_price = recent_trades[-1].price
        triggered, remaining = [], []
        for o in self._stop_orders:
            hit = ((o.side == OrderSide.BUY and last_price >= o.stop_price) or
                   (o.side == OrderSide.SELL and last_price <= o.stop_price))
            (triggered if hit else remaining).append(o)
        self._stop_orders = remaining
        for o in triggered:
            o.status = OrderStatus.OPEN
            o.order_type = OrderType.MARKET
            new_trades = self.submit_order(o)
            self._trade_log.extend(new_trades)

    def _add_to_book(self, o):
        if o.side == OrderSide.BUY:
            k = -o.price
            if k not in self._bids:
                self._bids[k] = []
            self._bids[k].append(o)
        else:
            k = o.price
            if k not in self._asks:
                self._asks[k] = []
            self._asks[k].append(o)

    def _remove_from_book(self, o):
        k, book = (-o.price,
                   self._bids) if o.side == OrderSide.BUY else (o.price, self._asks)
        if k in book:
            book[k] = [x for x in book[k] if x.order_id != o.order_id]
            if not book[k]:
                del book[k]

    def _can_fill_fully(self, o) -> bool:
        needed = o.quantity
        book_side = self._asks if o.side == OrderSide.BUY else self._bids
        for price_key, queue in book_side.items():
            rp = -price_key if o.side == OrderSide.BUY else price_key
            if rp <= 0:
                continue
            if o.price:
                if o.side == OrderSide.BUY and rp > o.price:
                    break
                if o.side == OrderSide.SELL and rp < o.price:
                    break
            for r in queue:
                needed -= r.remaining_qty
                if needed <= 0:
                    return True
        return False

    def _validate(self, o):
        if o.quantity <= 0:
            raise ValueError("Quantity must be positive")
        if o.order_type in (OrderType.LIMIT, OrderType.IOC,
                            OrderType.FOK,   OrderType.GTC):
            if o.price is None:
                raise ValueError(f"{o.order_type.value} needs a price")
        if o.order_type == OrderType.STOP:
            if o.stop_price is None:
                raise ValueError("STOP needs stop_price")

    def seed_from_nbbo(self, nbbo: NBBOQuote, levels: int = 5):
        """
        Replaces the book's resting orders with synthetic liquidity
        built from the live NBBO. Called on every quote update.

        The bid side is built downward from nbbo.bid_price,
        the ask side upward from nbbo.ask_price.
        This gives the strategy realistic prices to fill against
        while the full exchange book isn't being replicated.
        """
        if nbbo.bid_price <= 0 or nbbo.ask_price <= 0:
            return
        self._bids = SortedDict()
        self._asks = SortedDict()
        tick = 0.01
        for i in range(1, levels + 1):
            bp = round(nbbo.bid_price - (i - 1) * tick, 4)
            ap = round(nbbo.ask_price + (i - 1) * tick, 4)
            bq = max(nbbo.bid_size / levels, 10)
            aq = max(nbbo.ask_size / levels, 10)
            if bp > 0:
                self._place_resting(OrderSide.BUY,  bp, bq)
            if ap > 0:
                self._place_resting(OrderSide.SELL, ap, aq)

    def _place_resting(self, side, price, qty):
        o = Order(self.symbol, side, OrderType.LIMIT, qty, price=price)
        self._orders[o.order_id] = o
        self._add_to_book(o)


# ══════════════════════════════════════════════════════════════════════════
#  RISK MANAGER
# ══════════════════════════════════════════════════════════════════════════

class RiskManager:
    def __init__(self, max_position=500.0, daily_loss_limit=-2000.0,
                 max_order_qty=100.0):
        self.max_position = max_position
        self.daily_loss_limit = daily_loss_limit
        self.max_order_qty = max_order_qty
        self.killed = False

    def check(self, order: Order, position: Position,
              price: float) -> tuple[bool, str]:
        if self.killed:
            return False, "KILL SWITCH ACTIVE"
        pnl = position.total_pnl(price)
        if pnl < self.daily_loss_limit:
            self.killed = True
            return False, f"DAILY LOSS LIMIT HIT ({pnl:.2f})"
        if order.quantity > self.max_order_qty:
            return False, f"ORDER TOO LARGE"
        if order.side == OrderSide.BUY:
            if position.qty + order.quantity > self.max_position:
                return False, "POSITION LIMIT"
        return True, "OK"


# ══════════════════════════════════════════════════════════════════════════
#  MEAN REVERSION STRATEGY  (buy low / sell high)
# ══════════════════════════════════════════════════════════════════════════

class MeanReversionStrategy:
    """
    Buy low / sell high via rolling z-score.
    Buys when price drops entry_z std devs below rolling MA.
    Exits via limit sell at +target_pct or stop at -stop_pct.
    """

    def __init__(self, book: OrderBook, position: Position,
                 risk: RiskManager, stats: MarketStats,
                 ma_period=3, entry_z=0.05, target_pct=0.005,
                 stop_pct=0.003, qty=10.0):
        self.book = book
        self.position = position
        self.risk = risk
        self.stats = stats
        self.ma_period = ma_period
        self.entry_z = entry_z
        self.target_pct = target_pct
        self.stop_pct = stop_pct
        self.qty = qty
        self._sell_oid = None
        self._stop_oid = None
        self._closes = deque(maxlen=500)
        self.log = []

    def on_quote(self, nbbo: NBBOQuote):
        mid = nbbo.mid_price
        if mid is None or mid <= 0:
            return

        self._closes.append(mid)

        if len(self._closes) < self.ma_period + 1:
            return

        cl = list(self._closes)
        ma = sum(cl[-self.ma_period:]) / self.ma_period
        mean = sum(cl) / len(cl)
        std = math.sqrt(sum((p - mean) ** 2 for p in cl) / len(cl))
        if std < 0.001:
            return

        z = (ma - mid) / std

        # ── Check exits ───────────────────────────────────────────────────
        if not self.position.is_flat:
            sell_o = self.book.get_order(
                self._sell_oid) if self._sell_oid else None
            stop_o = self.book.get_order(
                self._stop_oid) if self._stop_oid else None

            if sell_o and sell_o.status == OrderStatus.FILLED:
                self.position.on_fill(
                    sell_o.price, sell_o.quantity, OrderSide.SELL)
                if self._stop_oid:
                    self.book.cancel_order(self._stop_oid)
                self._sell_oid = self._stop_oid = None
                msg = f"TARGET HIT @ {sell_o.price:.4f} | PnL={self.position.realised_pnl:+.2f}"
                log.info(f"  [MR] {msg}")
                self.log.append(msg)
                return

            if stop_o and stop_o.status == OrderStatus.FILLED:
                fill_p = nbbo.bid_price or mid
                self.position.on_fill(fill_p, self.qty, OrderSide.SELL)
                if self._sell_oid:
                    self.book.cancel_order(self._sell_oid)
                self._sell_oid = self._stop_oid = None
                msg = f"STOP HIT @ {fill_p:.4f} | PnL={self.position.realised_pnl:+.2f}"
                log.info(f"  [MR] {msg}")
                self.log.append(msg)
                return

        # ── Entry ─────────────────────────────────────────────────────────
        if self.position.is_flat and z >= self.entry_z:
            buy = Order(self.book.symbol, OrderSide.BUY,
                        OrderType.MARKET, self.qty)
            allowed, reason = self.risk.check(buy, self.position, mid)
            if not allowed:
                log.warning(f"  [MR] BLOCKED: {reason}")
                return
            trades = self.book.submit_order(buy)
            if trades:
                entry = trades[-1].price
                self.position.on_fill(entry, self.qty, OrderSide.BUY)
                target = round(entry * (1 + self.target_pct), 4)
                stop_p = round(entry * (1 - self.stop_pct),   4)
                sell = Order(self.book.symbol, OrderSide.SELL,
                             OrderType.LIMIT, self.qty, price=target)
                stop = Order(self.book.symbol, OrderSide.SELL,
                             OrderType.STOP,  self.qty, stop_price=stop_p)
                self.book.submit_order(sell)
                self.book.submit_order(stop)
                self._sell_oid = sell.order_id
                self._stop_oid = stop.order_id
                msg = f"BUY @ {entry:.4f} z={z:.2f} target={target:.4f} stop={stop_p:.4f}"
                log.info(f"  [MR] {msg}")
                self.log.append(msg)

# ══════════════════════════════════════════════════════════════════════════
#  MASSIVE.IO WEBSOCKET CLIENT
# ══════════════════════════════════════════════════════════════════════════


class MassiveClient:
    """
    Free-tier compatible: replays historical minute bars from Massive
    through the strategy engine bar by bar.

    Each bar provides: open, high, low, close, volume, vwap.
    We simulate bid/ask from the close price and feed it to the strategy.
    """

    def __init__(self, api_key: str, tickers: list,
                 book, stats, nbbo, strategy,
                 days_back: int = 1, delayed: bool = True):
        self.tickers = tickers
        self.book = book
        self.stats = stats
        self.nbbo = nbbo
        self.strategy = strategy
        self.days_back = days_back
        self._rest = RESTClient(api_key=api_key)
        self._tick_count = 0
        self.stats = strategy.stats

    def run(self):
        ticker = self.tickers[0]
        date_to = date.today() - timedelta(days=1)      # yesterday
        date_from = date_to - timedelta(days=self.days_back)

        log.info(f"Fetching minute bars for {ticker} "
                 f"from {date_from} to {date_to}...")

        bars = []
        for bar in self._rest.list_aggs(
            ticker=ticker,
            multiplier=1,
            timespan="minute",
            from_=str(date_from),
            to=str(date_to),
            limit=50000,
            adjusted=True,
        ):
            bars.append(bar)

        if not bars:
            log.error("No bars returned — market may have been closed "
                      "or date range is invalid.")
            return

        log.info(f"Replaying {len(bars)} bars through strategy...")
        print()

        last_price = float(bars[0].close)

        for i, bar in enumerate(bars):
            close = float(bar.close)
            high = float(bar.high)
            low = float(bar.low)
            volume = float(bar.volume)
            vwap = float(bar.vwap) if bar.vwap else close
            last_price = close

            # 1. Feed stats
            stats = self.strategy.stats
            stats.on_trade(high,  volume * 0.4, OrderSide.BUY)
            stats.on_trade(low,   volume * 0.4, OrderSide.SELL)
            stats.on_trade(close, volume * 0.2,
                           OrderSide.BUY if close >= vwap else OrderSide.SELL)

            # 2. Build NBBO
            spread = max(round(close * 0.0001, 2), 0.01)
            nbbo = NBBOQuote(
                symbol=ticker,
                bid_price=round(close - spread / 2, 4),
                ask_price=round(close + spread / 2, 4),
                bid_size=volume * 0.5,
                ask_size=volume * 0.5,
            )
            if i == 0:
                log.info(
                    f"  [DEBUG] nbbo: bid={nbbo.bid_price} ask={nbbo.ask_price} mid={nbbo.mid_price} close={close}")

            # 3. Reseed book and run strategy
            self.strategy.book.seed_from_nbbo(nbbo)
            self.strategy.on_quote(nbbo)
            self._tick_count += 1

            # 4. Progress dot every 100 bars
            if (i + 1) % 100 == 0:
                print(".", end="", flush=True)

        # ← final report sits HERE — outside the loop, same indent as `for`
        print()
        pos = self.strategy.position
        risk = self.strategy.risk        # ← fixes the risk error
        stats = self.strategy.stats       # ← reads from the same stats that was updated
        log.info("─── FINAL REPORT ─────────────────────────────────────")
        log.info(f"  Ticker       : {ticker}")
        log.info(f"  Bars replayed: {self._tick_count}")
        log.info(f"  Session High : {stats.high:.4f}")
        log.info(f"  Session Low  : {stats.low:.4f}")
        log.info(
            f"  VWAP         : {stats.vwap:.4f}" if stats.vwap else "  VWAP         : –")
        log.info(f"  Buy Volume   : {stats.buy_volume:.0f}")
        log.info(f"  Sell Volume  : {stats.sell_volume:.0f}")
        log.info(f"  Delta        : {stats.delta:+.0f}")
        log.info(f"  Total trades : {pos.trade_count}")
        log.info(f"  Position     : {pos.qty:.0f} shares")
        log.info(f"  Avg cost     : {pos.avg_cost:.4f}")
        log.info(f"  Realised PnL : {pos.realised_pnl:+.2f}")
        log.info(f"  Unrealised   : {pos.unrealised_pnl(last_price):+.2f}")
        log.info(f"  Total PnL    : {pos.total_pnl(last_price):+.2f}")
        log.info(f"  Kill switch  : {'⚠ ACTIVE' if risk.killed else 'OFF'}")
        log.info("──────────────────────────────────────────────────────")
# ══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════


def build_system(tickers: list[str]) -> tuple:
    """Wires all components together for a given ticker list."""
    symbol = tickers[0]          # primary symbol drives the strategy
    book = OrderBook(symbol)
    position = Position(symbol)
    stats = MarketStats(window=200)
    risk = RiskManager(
        max_position=500,    # max 500 shares long
        daily_loss_limit=-2000,  # stop all trading if down $2k
        max_order_qty=100,    # no single order > 100 shares
    )
    nbbo = NBBOQuote(symbol=symbol)
    strategy = MeanReversionStrategy(
        book=book,
        position=position,
        risk=risk,
        stats=stats,
        ma_period=3,
        entry_z=0.05,
        target_pct=0.005,  # was 0.015 — tighter target (0.5%) for minute data
        stop_pct=0.003,  # was 0.008 — tighter stop (0.3%)
        qty=10,     # unchanged
    )
    return book, stats, nbbo, strategy


def main(api_key: str, tickers: list, delayed: bool):
    log.info(f"Starting | tickers={tickers} | days_back=5")
    for ticker in tickers:
        log.info(f"\n{'='*54}\n  Running strategy on {ticker}\n{'='*54}")
        book, stats, nbbo, strategy = build_system([ticker])
        client = MassiveClient(
            api_key=api_key,
            tickers=[ticker],
            book=book,
            stats=stats,
            nbbo=nbbo,
            strategy=strategy,
            days_back=5,
        )
        client.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", default=["NVDA"])
    parser.add_argument(
        "--mode", choices=["live", "delayed"], default="delayed")
    args = parser.parse_args()

    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if not api_key:
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            for line in open(env_path):
                if line.startswith("MASSIVE_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"')
                    break

    if not api_key:
        log.error("No API key found. Set MASSIVE_API_KEY in your .env file.")
        exit(1)

    log.info(f"API key loaded ({api_key[:6]}...{api_key[-4:]})")
    main(api_key, args.tickers, delayed=(args.mode == "delayed"))
