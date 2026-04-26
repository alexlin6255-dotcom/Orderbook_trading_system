# OrderBook Trading System

A full-stack algorithmic trading system built from scratch — limit orderbook engine, mean-reversion strategy, real market data via Massive.com, and a live browser dashboard.

![Dashboard](https://img.shields.io/badge/status-active-green) ![Python](https://img.shields.io/badge/python-3.9+-blue) ![License](https://img.shields.io/badge/license-MIT-blue)

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/alexlin6255-dotcom/Orderbook_trading_system.git
cd Orderbook_trading_system/orderbook.py

# 2. Start the proxy server
python proxy.py

# 3. Open your browser and go to:
#    http://127.0.0.1:8888/trading_dashboard.html
```

Then paste your free Massive.com API key into the dashboard and enter any ticker.

## Requirements

- Python 3.9+ (no extra packages needed)
- A free API key from [massive.com](https://massive.com) — sign up takes 2 minutes

## What it does

Fetches real 5-day historical minute bar data from Massive.com for any ticker, replays a mean-reversion strategy through a custom orderbook engine, and displays results in an interactive dashboard.

## Supported symbols

Any ticker on Massive.com:

| Type | Examples |
|------|---------|
| US Stocks | `NVDA`, `TSLA`, `AAPL`, `MSFT`, `AMZN` |
| ETFs | `SPY`, `QQQ`, `DIA`, `GLD`, `IWM` |
| Crypto | `X:BTCUSD`, `X:ETHUSD` |
| Forex | `C:EURUSD`, `C:GBPUSD` |

## Components

- **Orderbook engine** — Price-time priority matching with 7 order types (Market, Limit, Stop, Stop-Limit, IOC, FOK, GTC)
- **Mean reversion strategy** — Buys when price drops below rolling average by a z-score threshold, exits via limit sell or stop loss
- **Position tracker** — Weighted average cost, realised/unrealised PnL
- **Risk manager** — Position limits, daily loss limit, kill switch
- **Dashboard** — Cumulative PnL chart, order flow, orderbook depth, trade log, multi-ticker comparison

## How it works

```
Massive.com API → proxy.py → strategy engine (JS) → dashboard
```

The proxy fetches real bar data from Massive, the strategy runs locally in your browser, and results are displayed instantly.

## Files

```
orderbook.py/
  ├── main.py                  # Core Python engine (orderbook + strategy)
  ├── proxy.py                 # Local server that fetches Massive data
  └── trading_dashboard.html  # Browser dashboard
```
