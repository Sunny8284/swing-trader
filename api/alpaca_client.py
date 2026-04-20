"""
api/alpaca_client.py — Alpaca trading and data clients for paper trading.
"""

import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient

# Load .env from parent directory
load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"

# Trading client — for account, positions, orders
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)

# Data client — for historical bars, quotes
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
