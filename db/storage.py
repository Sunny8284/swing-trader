"""
db/storage.py — SQLite persistence layer using SQLAlchemy.

Tables
──────
signals      : Every signal generated (BUY/SELL/HOLD) with indicator values.
trades       : Every order submitted to Alpaca (entry, exit, P&L).
portfolio_snapshots : Daily portfolio value snapshots for tracking performance.
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column, DateTime, Float, Integer, String, Text, create_engine, text
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

import config

logger = logging.getLogger(__name__)

engine = create_engine(
    config.DATABASE_URL,
    connect_args={"check_same_thread": False},  # needed for SQLite + multi-thread
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


# ── ORM Models ─────────────────────────────────────────────────────────────────

class SignalRecord(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    ticker = Column(String(10), nullable=False, index=True)
    signal = Column(String(4), nullable=False)   # BUY / SELL / HOLD
    score = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    rsi = Column(Float)
    macd = Column(Float)
    sma20 = Column(Float)
    sma50 = Column(Float)
    sma200 = Column(Float)
    bb_upper = Column(Float)
    bb_lower = Column(Float)
    reasons = Column(Text)   # pipe-separated list of reason strings


class TradeRecord(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    ticker = Column(String(10), nullable=False, index=True)
    side = Column(String(4), nullable=False)    # buy / sell
    qty = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    order_id = Column(String(64))               # Alpaca order ID
    status = Column(String(20), default="submitted")
    # Populated when position is closed
    exit_price = Column(Float)
    pnl = Column(Float)
    closed_at = Column(DateTime)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    equity = Column(Float, nullable=False)      # total portfolio value
    cash = Column(Float, nullable=False)
    buying_power = Column(Float, nullable=False)
    position_count = Column(Integer, default=0)


# ── Init ───────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create all tables if they don't exist yet."""
    Base.metadata.create_all(bind=engine)
    logger.info("Database initialised at %s", config.DATABASE_URL)


# ── Write helpers ──────────────────────────────────────────────────────────────

def save_signal(result) -> SignalRecord:
    """Persist a SignalResult to the signals table."""
    with SessionLocal() as session:
        record = SignalRecord(
            ticker=result.ticker,
            signal=result.signal.value,
            score=result.score,
            price=result.price,
            rsi=result.rsi,
            macd=result.macd,
            sma20=result.sma20,
            sma50=result.sma50,
            sma200=result.sma200,
            bb_upper=result.bb_upper,
            bb_lower=result.bb_lower,
            reasons=" | ".join(result.reasons),
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record


def save_trade(
    ticker: str,
    side: str,
    qty: float,
    entry_price: float,
    order_id: Optional[str] = None,
    status: str = "submitted",
) -> TradeRecord:
    """Persist a new trade order to the trades table."""
    with SessionLocal() as session:
        record = TradeRecord(
            ticker=ticker,
            side=side,
            qty=qty,
            entry_price=entry_price,
            order_id=order_id,
            status=status,
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        logger.info("Trade saved: %s %s x%.2f @ %.2f", side.upper(), ticker, qty, entry_price)
        return record


def close_trade(ticker: str, exit_price: float, pnl: float) -> None:
    """Mark the most recent open buy for `ticker` as closed with P&L."""
    with SessionLocal() as session:
        record = (
            session.query(TradeRecord)
            .filter(
                TradeRecord.ticker == ticker,
                TradeRecord.side == "buy",
                TradeRecord.closed_at.is_(None),
            )
            .order_by(TradeRecord.created_at.desc())
            .first()
        )
        if record:
            record.exit_price = exit_price
            record.pnl = pnl
            record.closed_at = datetime.utcnow()
            record.status = "closed"
            session.commit()
            logger.info("Trade closed: %s exit=%.2f pnl=%.2f", ticker, exit_price, pnl)


def get_trade_stats() -> dict:
    """Win rate and P&L summary from closed trades."""
    with SessionLocal() as session:
        closed = (
            session.query(TradeRecord)
            .filter(TradeRecord.closed_at.isnot(None), TradeRecord.pnl.isnot(None))
            .all()
        )
        total = len(closed)
        if total == 0:
            return {"total_closed": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0}
        wins = sum(1 for t in closed if (t.pnl or 0) > 0)
        total_pnl = sum(t.pnl or 0 for t in closed)
        return {
            "total_closed": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate": round(wins / total * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / total, 2),
        }


def save_portfolio_snapshot(
    equity: float,
    cash: float,
    buying_power: float,
    position_count: int,
) -> PortfolioSnapshot:
    """Record the current portfolio state."""
    with SessionLocal() as session:
        snap = PortfolioSnapshot(
            equity=equity,
            cash=cash,
            buying_power=buying_power,
            position_count=position_count,
        )
        session.add(snap)
        session.commit()
        session.refresh(snap)
        return snap


# ── Read helpers ───────────────────────────────────────────────────────────────

def get_recent_signals(limit: int = 50) -> list[SignalRecord]:
    with SessionLocal() as session:
        return (
            session.query(SignalRecord)
            .order_by(SignalRecord.created_at.desc())
            .limit(limit)
            .all()
        )


def get_recent_trades(limit: int = 50) -> list[TradeRecord]:
    with SessionLocal() as session:
        return (
            session.query(TradeRecord)
            .order_by(TradeRecord.created_at.desc())
            .limit(limit)
            .all()
        )


def get_open_trades() -> list[TradeRecord]:
    """Return trades that haven't been closed yet."""
    with SessionLocal() as session:
        return (
            session.query(TradeRecord)
            .filter(TradeRecord.closed_at.is_(None))
            .order_by(TradeRecord.created_at.desc())
            .all()
        )


def get_portfolio_history(limit: int = 30) -> list[PortfolioSnapshot]:
    with SessionLocal() as session:
        return (
            session.query(PortfolioSnapshot)
            .order_by(PortfolioSnapshot.created_at.desc())
            .limit(limit)
            .all()
        )
