# OrderBook Trading System

A complete algorithmic trading system built entirely from scratch in Python and JavaScript. It implements a real price-time priority matching engine, a mean-reversion strategy with position tracking and risk management, live market data integration via Massive.com, and an interactive browser dashboard — no external trading libraries used.

---

## Table of Contents

- [Overview](#overview)
- [Quick Start](#quick-start)
- [How It Works](#how-it-works)
- [The Orderbook Engine](#the-orderbook-engine)
- [The Strategy](#the-strategy)
- [Market Data](#market-data)
- [The Dashboard](#the-dashboard)
- [Supported Symbols](#supported-symbols)
- [File Structure](#file-structure)
- [Key Concepts](#key-concepts)

---

## Overview

Most trading system tutorials use pre-built libraries. This project builds everything from first principles:

- A **limit orderbook** that matches buyers and sellers using price-time priority — the same algorithm used by real exchanges
- A **mean-reversion strategy** that buys when price drops statistically below its rolling average and exits via limit sell or stop loss
- A **position tracker** that maintains weighted average cost basis and computes realised and unrealised profit/loss in real time
- A **risk manager** that enforces position limits, daily loss caps, and a kill switch
- A **live dashboard** that fetches real historical minute bar data from Massive.com and displays results in the browser
<img width="1512" height="982" alt="Screenshot 2026-04-26 at 9 08 37 PM" src="https://github.com/user-attachments/assets/68cdb263-66d3-49c9-ae67-9fde1ac2f251" />



---

## Quick Start

**Requirements:** Python 3.9 or higher. No extra packages needed — the proxy uses only the Python standard library.

**Step 1 — Clone the repository**

```bash
git clone https://github.com/alexlin6255-dotcom/Orderbook_trading_system.git
cd Orderbook_trading_system/orderbook.py
```

**Step 2 — Get a free Massive.com API key**

Go to [massive.com](https://massive.com), create a free account, and copy your API key from the dashboard. The free tier includes access to historical aggregate bar data which is all this system needs.

**Step 3 — Start the local proxy server**

```bash
python proxy.py
```

You should see:
```
====================================================
  OrderBook Dashboard Proxy
====================================================
  Dashboard:  http://127.0.0.1:8888/trading_dashboard.html
  Keep this terminal open. Press Ctrl+C to stop.
====================================================
```

**Step 4 — Open the dashboard**

Open your browser and go to:
```
http://127.0.0.1:8888/trading_dashboard.html
```

**Step 5 — Run the strategy**

1. Paste your Massive.com API key into the key field at the top
2. Enter any ticker symbols (comma separated) — e.g. `NVDA, TSLA, AAPL`
3. Click **Run Strategy**

Results appear within a few seconds.

> **Note:** Use Chrome or Firefox. Open the dashboard via `http://127.0.0.1:8888` — not by opening the HTML file directly and not via Live Server. The proxy must be running for the dashboard to fetch data.

---

## How It Works

```
Massive.com REST API
        │
        ▼
   proxy.py (local Python server)
   - Authenticates with your API key
   - Fetches 5 days of 1-minute bar data
   - Returns OHLCV bars to the browser
        │
        ▼
   trading_dashboard.html (browser)
   - Runs the mean-reversion strategy in JavaScript
   - Replays all bars through the strategy logic
   - Computes entries, exits, PnL, z-scores
   - Renders charts and trade log
```

The proxy is necessary because browsers cannot call `api.massive.com` directly due to CORS (cross-origin resource sharing) security restrictions. The proxy acts as a local middleman — your API key never leaves your machine.

---

## The Orderbook Engine

The core engine (`main.py`) implements a real limit orderbook with price-time priority matching.

### Data Structures

Bids and asks are stored in sorted dictionaries keyed by price. Bids use negated keys so the highest bid is always first. Within each price level, orders are stored in a queue (FIFO) so the oldest order at a given price fills first.

```
ASK $100.30  [Order C (t=2)]
ASK $100.20  [Order A (t=0), Order B (t=1)]   ← best ask, FIFO within level
────────────── spread ──────────────────────
BID  $99.80  [Order D (t=1), Order E (t=3)]   ← best bid
BID  $99.70  [Order F (t=0)]
```

### Order Types

| Type | Behaviour |
|------|-----------|
| **Market** | Executes immediately at best available price. No price guarantee. |
| **Limit** | Executes at specified price or better. Rests in book if not immediately filled. |
| **Stop** | Waits until last trade price crosses stop_price, then converts to Market. |
| **Stop-Limit** | Like Stop, but converts to Limit instead of Market. Gives price control. |
| **IOC** | Immediate-Or-Cancel. Fills what it can instantly, cancels the rest. |
| **FOK** | Fill-Or-Kill. Must fill entirely or cancel entirely. No partial fills. |
| **GTC** | Good-Till-Cancelled. Identical to Limit but persists until manually cancelled. |

### Matching Engine

When an order arrives, the engine walks the opposite side of the book from best price inward, consuming resting orders FIFO until the aggressor is filled or no more eligible counterparts exist. Every fill generates a `Trade` object recording price, quantity, and which orders were involved.

Stop orders sit in a separate list and are checked after every batch of trades. If the last trade price crosses a stop order's trigger price, the stop is promoted to a live Market or Limit order and re-entered into the matching engine.

### Order Lifecycle

```
OPEN → PARTIALLY_FILLED → FILLED
                        → CANCELLED  (manual cancel, or IOC/FOK automatic)
     → PENDING          (stop orders waiting for trigger)
     → REJECTED         (failed validation)
```

---

## The Strategy

The mean-reversion strategy is based on the statistical principle that prices tend to revert toward their recent average after moving too far in one direction.

### Entry Logic

On every bar, the strategy computes:

1. A **rolling moving average (MA)** of the last N closing prices
2. The **standard deviation** of the full price history window
3. A **z-score**: how many standard deviations the current price is below the MA

```
z = (MA - current_price) / std_dev
```

When `z >= entry_threshold` (price is statistically cheap relative to recent history), the strategy submits a Market Buy order.

### Exit Logic

After entering, two resting orders are placed simultaneously:

- A **Limit Sell** at `entry_price × (1 + target_pct)` — take profit if price recovers
- A **Stop Sell** at `entry_price × (1 - stop_pct)` — cut the loss if price falls further

Whichever triggers first closes the position. The other is cancelled.

### Position Tracking

Every fill updates the position using weighted average cost:

```
new_avg_cost = (old_avg × old_qty + fill_price × fill_qty) / new_qty
```

Realised PnL is locked in on each sell:

```
realised_pnl += (sell_price - avg_cost) × qty_sold
```

Unrealised PnL is computed against the current market price for any open position.

### Risk Management

The risk manager sits in front of every order submission and enforces:

- **Max position size** — never hold more than N shares
- **Daily loss limit** — if total PnL drops below threshold, activate kill switch
- **Kill switch** — once active, all further orders are blocked for the session
- **Max order size** — no single order can exceed a set quantity

---

## Market Data

Data is fetched from Massive.com (formerly Polygon.io) using their `/v2/aggs` REST endpoint. Each bar contains:

| Field | Description |
|-------|-------------|
| `o` | Open price |
| `h` | High price |
| `l` | Low price |
| `c` | Close price |
| `v` | Volume (shares traded) |
| `vw` | Volume-weighted average price (VWAP) |
| `t` | Timestamp (Unix milliseconds) |

The system fetches 1-minute bars over the last 8 calendar days (approximately 5 trading days). For each bar, it:

1. Updates session high and low
2. Classifies volume as buy or sell based on whether close was above or below VWAP
3. Computes a running VWAP and order flow delta (buy volume minus sell volume)
4. Feeds the close price into the strategy

<img width="1512" height="982" alt="Screenshot 2026-04-26 at 9 09 02 PM" src="https://github.com/user-attachments/assets/554253e4-6a2b-4c76-b739-1451d817fb98" />

---

## The Dashboard

The browser dashboard (`trading_dashboard.html`) runs the strategy entirely in JavaScript after receiving the raw bar data from the proxy. No data is sent to any external server after the initial Massive API call.

### Panels

**Metric cards (top row)**
- Total P&L (realised + unrealised)
- Realised P&L from closed trades
- Number of trade entries
- Win rate (target hits / total entries)
- Session high and low

**Cumulative P&L chart**
- Each point represents one trade event (entry or exit)
- Line segments are green when PnL is rising, red when falling
- Zero line shown as a dashed reference
- Hover for exact values

**Order flow chart**
- Horizontal bar chart showing buy volume vs sell volume as percentages
- Net delta shown below (positive = net buying pressure)

**Trade log**
- Every BUY entry with its price, z-score, target, and stop
- Every TARGET HIT or STOP HIT with running P&L
- Scrollable, colour-coded by outcome

**Order book depth**
- Top 5 bid and ask levels around the last bar's closing price
- Bar lengths represent relative size at each level
- Spread shown between sides

**Status bar**
- Detected market regime, VWAP, volumes, delta, kill switch status

**Multi-ticker comparison**
- When multiple tickers are run, an All tab shows a side-by-side comparison table
- Click any row to drill into that ticker's full dashboard

## Order Book Ladder
The order book panel shows the full bid/ask ladder in the same format used by professional trading terminals. Asks (sellers) sit above the spread in red, bids (buyers) sit below in green. Each row shows the price level and the total quantity resting there, with a horizontal bar whose width is proportional to that level's size relative to the largest order in the book — making liquidity walls instantly visible without reading the numbers.
The dividing row between bids and asks shows the current spread (best ask minus best bid) and the mid-price (their average), which is the theoretical fair value of the asset at that moment.
Below the ladder, three summary statistics are shown:

## Bid depth — total shares/contracts resting on the buy side
Ask depth — total shares/contracts resting on the sell side
Imbalance — the percentage skew toward buyers or sellers, computed as (bid_depth - ask_depth) / (bid_depth + ask_depth). A reading of 70% bid-heavy means buyers have significantly more resting liquidity than sellers, which can indicate upward price pressure.

## <img width="1512" height="982" alt="Screenshot 2026-04-26 at 7 57 51 PM" src="https://github.com/user-attachments/assets/06b368cd-55d3-4bd5-ad54-4b098b681827" />
Price Distribution Chart
Below the ladder is a bar chart showing order size at every price level across the full book. Bids and asks are rendered as side-by-side bars at each level, giving a complete picture of the liquidity profile. Key things to look for:

Tall spikes at specific price levels indicate large resting orders — these act as support (on the bid side) or resistance (on the ask side) and often cause price to slow or reverse when approached
Thin levels with small bars show price zones where the market can move quickly with little friction
Asymmetry between bid and ask bars at the same price level reveals where one side has committed significantly more liquidity

---

## Supported Symbols

Any symbol available on Massive.com works. Common formats:

| Asset Class | Format | Examples |
|------------|--------|---------|
| US Stocks | `TICKER` | `NVDA`, `TSLA`, `AAPL`, `MSFT`, `AMZN`, `GOOGL` |
| ETFs | `TICKER` | `SPY`, `QQQ`, `DIA`, `IWM`, `GLD`, `SLV`, `USO` |
| Crypto | `X:BASEQUOTE` | `X:BTCUSD`, `X:ETHUSD`, `X:SOLUSD` |
| Forex | `C:BASEQUOTE` | `C:EURUSD`, `C:GBPUSD`, `C:USDJPY` |
| Indices | `I:INDEX` | `I:SPX`, `I:NDX`, `I:DJI` |

> The free Massive tier includes historical aggregate data. Real-time streaming and some premium endpoints require a paid plan.

---

## File Structure

```
orderbook.py/
├── main.py                   # Python orderbook engine + strategy
│   ├── OrderBook             # Matching engine (7 order types)
│   ├── Position              # Cost basis and PnL tracking
│   ├── MarketStats           # High, low, VWAP, volume, delta
│   ├── RiskManager           # Position limits and kill switch
│   ├── Strategy              # Mean-reversion entry/exit logic
│   └── MassiveClient         # Massive.com REST API integration
│
├── proxy.py                  # Local HTTP server
│   ├── GET /                 # Serves trading_dashboard.html
│   └── POST /fetch-bars      # Fetches bar data from Massive API
│
├── trading_dashboard.html    # Single-file browser dashboard
│   ├── Strategy engine       # JS port of the mean-reversion logic
│   ├── Chart rendering       # Chart.js PnL and flow charts
│   ├── Orderbook display     # Depth visualisation
│   └── Multi-ticker tabs     # Comparison table
│
└── .gitignore                # Excludes .env, __pycache__, .DS_Store
```

---

## Key Concepts

**Price-time priority** — The fundamental rule of most orderbooks. Among orders at the same price, the one submitted earliest fills first. This rewards liquidity providers who commit early.

**Mean reversion** — The tendency for asset prices to return toward their historical average after large moves. Works best in range-bound, low-trend markets. Fails in strongly trending markets where prices keep moving in one direction.

**Z-score** — A measure of how many standard deviations a value is from the mean. A z-score of 2.0 means the price is 2 standard deviations below average — statistically unusual and a potential reversion signal.

**Spread** — The difference between the best ask and best bid. The cost of immediately buying then selling. Tighter spreads indicate more liquid markets.

**VWAP** — Volume-Weighted Average Price. The average transaction price weighted by how much traded at each level. Used as a benchmark — buying below VWAP means paying less than the average participant did that day.

**Delta** — Buy volume minus sell volume. A large positive delta indicates aggressive buying pressure. Used by traders to gauge directional intent beyond what price alone shows.

**Slippage** — The difference between the expected fill price and the actual fill price. For large market orders that sweep multiple price levels, slippage compounds as cheaper levels are exhausted and the order fills at progressively worse prices.

---

## License

MIT — free to use, modify, and distribute.

