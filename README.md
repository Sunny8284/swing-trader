# Swing Trader 🤖

An automated swing trading system for US equities running entirely on a local Mac. Uses technical analysis signals to place paper trades on Alpaca, with a live dashboard and Telegram alerts.

> **Paper trading only** — all trades use Alpaca's paper trading environment (fake money).

---

## What It Does

Every weekday at **9:35 AM**, **12:00 PM**, and **3:30 PM ET**, the system automatically:

1. Fetches 6 months of daily price data for 20 watched stocks (via Yahoo Finance)
2. Scores each stock using 4 technical indicators — BUY if score ≥ +1, SELL if ≤ -1
3. Applies portfolio guardrails (position limits, cash reserve, daily loss limit)
4. Places bracket orders on Alpaca with automatic stop-loss (−2%) and take-profit (+6%)
5. Sends a Telegram message summarising the cycle
6. Updates a live web dashboard with portfolio state

The trading bot runs via **launchd** (macOS native scheduler) — no cloud servers, no monthly fees.

---

## Architecture

```
Mac (always-on during market hours)
├── launchd scheduler (3× weekday)
│   └── main.py run
│       ├── data/fetcher.py       ← yfinance (Yahoo Finance)
│       ├── signals/generator.py  ← RSI, MACD, SMA, Bollinger
│       ├── agent/trader.py       ← guardrails + execution decisions
│       ├── executor/trade_executor.py ← Alpaca paper orders
│       ├── db/storage.py         ← SQLite via SQLAlchemy
│       └── agent/notifier.py     ← Telegram alerts
│
└── dashboard (always-on)
    ├── api/server.py             ← FastAPI on :8000
    ├── Cloudflare Quick Tunnel   ← exposes :8000 publicly
    └── Vercel (Next.js)          ← swing-trader-dashboard.vercel.app
```

---

## Watchlist (20 tickers)

| Sector | Tickers |
|--------|---------|
| Tech | AAPL, MSFT, GOOGL, NVDA, AMD, TSLA, META, AMZN |
| Finance | JPM, BAC, V, MA |
| Healthcare | JNJ, UNH |
| Energy | XOM, CVX |
| ETFs | SPY, QQQ |
| Small-cap AI/Quantum | BBAI, RGTI |

---

## Signal Logic

Each ticker gets a composite score from 4 sub-signals:

| Indicator | BUY (+1) | SELL (−1) |
|-----------|----------|-----------|
| RSI (14) | RSI < 30 (oversold) | RSI > 70 (overbought) |
| MACD | MACD line crosses above signal | MACD line crosses below signal |
| SMA crossover | SMA20 > SMA50 (bullish) | SMA20 < SMA50 (bearish) |
| Bollinger Bands | Price < lower band | Price > upper band |

**Threshold:** score ≥ +1 → BUY, score ≤ −1 → SELL

---

## Risk Management

| Parameter | Value |
|-----------|-------|
| Max position size | 5% of portfolio per ticker |
| Stop-loss | −2% from entry |
| Take-profit | +6% from entry |
| Min cash reserve | 20% always kept in cash |
| Daily loss limit | Stops trading if portfolio down >3% in a day |

Orders are placed as **bracket orders** — Alpaca automatically closes the position when either TP or SL is hit (GTC, no manual intervention needed).

---

## Key Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point — `run`, `api`, `status`, `schedule` modes |
| `config.py` | All runtime settings (watchlist, thresholds, risk params) |
| `data/fetcher.py` | `fetch_ohlcv()` — downloads OHLCV bars via yfinance |
| `signals/generator.py` | `generate_signals()` — computes RSI/MACD/SMA/BB scores |
| `agent/trader.py` | `TradingAgent.run_cycle()` — applies guardrails, calls executor |
| `agent/notifier.py` | `send_cycle_summary()` — Telegram alerts after each cycle |
| `agent/reasoner.py` | Optional Groq AI reasoning attached to signals |
| `executor/trade_executor.py` | `execute_buy()` / `execute_sell()` — Alpaca order placement |
| `api/server.py` | FastAPI app — `/api/portfolio`, `/api/signals`, `/api/trades` |
| `api/alpaca_client.py` | Alpaca SDK client (paper trading) |
| `db/storage.py` | SQLite ORM — saves signals, trades, portfolio snapshots |
| `backtest/engine.py` | `run()` — vectorised backtest engine |
| `backtest/variants.py` | Compares strategy variants (V0 baseline → V6) |
| `dashboard_keepalive.sh` | Keeps FastAPI + Cloudflare tunnel alive via launchd |

---

## Setup

### 1. Clone & install dependencies

```bash
git clone https://github.com/Sunny8284/swing-trader.git
cd swing-trader
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

Create a `.env` file in the project root (never committed):

```
ALPACA_API_KEY=your_alpaca_paper_key
ALPACA_SECRET_KEY=your_alpaca_paper_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
GROQ_API_KEY=your_groq_key          # optional — AI signal reasoning
DATABASE_URL=sqlite:///swing_trader.db
```

Get Alpaca paper keys at [alpaca.markets](https://alpaca.markets) (free account).

### 3. Run manually

```bash
# One trading cycle immediately
venv/bin/python main.py run

# Check account and positions
venv/bin/python main.py status

# Start the dashboard API server
venv/bin/python main.py api

# Run backtests across strategy variants
venv/bin/python -m backtest.variants --days 365
```

### 4. Automate with launchd (macOS)

Copy the plists to `~/Library/LaunchAgents/` and load them:

```bash
cp com.nithun.swingtrader.plist ~/Library/LaunchAgents/
cp com.nithun.dashboardtunnel.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.nithun.swingtrader.plist
launchctl load ~/Library/LaunchAgents/com.nithun.dashboardtunnel.plist
```

---

## Backtest Results (as of May 2026)

| Strategy | 1-Year Return | Sharpe | Max Drawdown |
|----------|--------------|--------|--------------|
| V0 Baseline | +17.1% | 2.73 | 2.2% |
| V4 Regime-aware RSI + diversified watchlist | +25.4% | 3.23 | 2.4% |
| SPY buy-and-hold | +30.5% | — | ~10%+ |

The strategy's edge is **risk-adjusted** (low drawdown), not raw return. It shines in sideways/bear regimes.

---

## API Keys & Security

- `.env` is in `.gitignore` — credentials are **never committed**
- All trading uses Alpaca **paper** (simulated) environment
- No real money is at risk
