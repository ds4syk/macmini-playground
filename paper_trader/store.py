"""SQLite storage layer."""
from __future__ import annotations

import sqlite3
from typing import Iterable, List, Optional, Tuple

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL NOT NULL,
    close REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    volume REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS portfolios (
    id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    capital REAL NOT NULL,
    cash REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    portfolio_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    shares INTEGER NOT NULL,
    avg_cost REAL NOT NULL,
    last_buy_date TEXT,
    PRIMARY KEY (portfolio_id, symbol)
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id TEXT NOT NULL,
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    shares INTEGER NOT NULL,
    price REAL NOT NULL,
    amount REAL NOT NULL,
    fee REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    reason TEXT,
    model_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_unique
    ON trades (portfolio_id, date, symbol, side, shares, price);
CREATE TABLE IF NOT EXISTS equity (
    portfolio_id TEXT NOT NULL,
    date TEXT NOT NULL,
    cash REAL NOT NULL,
    market_value REAL NOT NULL,
    total REAL NOT NULL,
    PRIMARY KEY (portfolio_id, date)
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL,
    date TEXT NOT NULL,
    targets_json TEXT NOT NULL,
    market_view TEXT,
    confidence REAL,
    raw TEXT,
    latency_ms INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    error TEXT,
    executed_date TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (model_id, date)
);
CREATE TABLE IF NOT EXISTS runs (
    date TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, kind)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def init_portfolios(conn: sqlite3.Connection) -> None:
    for model in config.MODELS:
        for capital in config.CAPITALS:
            conn.execute(
                "INSERT OR IGNORE INTO portfolios (id, model_id, capital, cash) VALUES (?, ?, ?, ?)",
                (config.portfolio_id(model["id"], capital), model["id"], float(capital), float(capital)),
            )
    conn.commit()


def reset_portfolios(conn: sqlite3.Connection) -> None:
    ids = [row["id"] for row in conn.execute("SELECT id FROM portfolios")]
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    conn.execute("DELETE FROM positions WHERE portfolio_id IN (%s)" % placeholders, ids)
    conn.execute("DELETE FROM trades WHERE portfolio_id IN (%s)" % placeholders, ids)
    conn.execute("DELETE FROM equity WHERE portfolio_id IN (%s)" % placeholders, ids)
    for item in config.MODELS:
        for capital in config.CAPITALS:
            conn.execute(
                "UPDATE portfolios SET cash = ? WHERE id = ?",
                (float(capital), config.portfolio_id(item["id"], capital)),
            )
    conn.commit()


def get_positions(conn: sqlite3.Connection, portfolio_id: str) -> List[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM positions WHERE portfolio_id = ? AND shares > 0 ORDER BY symbol",
            (portfolio_id,),
        )
    )


def get_position(
    conn: sqlite3.Connection, portfolio_id: str, symbol: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM positions WHERE portfolio_id = ? AND symbol = ?",
        (portfolio_id, symbol),
    ).fetchone()


def bulk_upsert_bars(conn: sqlite3.Connection, rows: Iterable[Tuple]) -> int:
    rows = list(rows)
    conn.executemany(
        """INSERT INTO bars (symbol, date, open, close, high, low, volume)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(symbol, date) DO UPDATE SET
             open=excluded.open, close=excluded.close, high=excluded.high,
             low=excluded.low, volume=excluded.volume""",
        rows,
    )
    conn.commit()
    return len(rows)


def bar_range(conn: sqlite3.Connection, symbol: str) -> Tuple[Optional[str], Optional[str]]:
    row = conn.execute(
        "SELECT MIN(date) AS lo, MAX(date) AS hi, COUNT(*) AS n FROM bars WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    if not row or not row["n"]:
        return None, None
    return row["lo"], row["hi"]


def bars_for(
    conn: sqlite3.Connection, symbol: str, end_date: Optional[str] = None, limit: int = 320
) -> List[sqlite3.Row]:
    if end_date:
        rows = conn.execute(
            "SELECT * FROM bars WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT ?",
            (symbol, end_date, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM bars WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
    return list(reversed(rows))


def bars_on(conn: sqlite3.Connection, date: str, symbols: Iterable[str]) -> dict:
    symbols = list(symbols)
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    rows = conn.execute(
        "SELECT * FROM bars WHERE date = ? AND symbol IN (%s)" % placeholders,
        [date] + symbols,
    ).fetchall()
    return {row["symbol"]: row for row in rows}


def last_trading_days(
    conn: sqlite3.Connection, start: str, end: str, symbol: str = "sh000001"
) -> List[str]:
    rows = conn.execute(
        "SELECT date FROM bars WHERE symbol = ? AND date >= ? AND date <= ? ORDER BY date",
        (symbol, start, end),
    ).fetchall()
    return [row["date"] for row in rows]


def save_equity(
    conn: sqlite3.Connection,
    portfolio_id: str,
    date: str,
    cash: float,
    market_value: float,
) -> None:
    conn.execute(
        """INSERT INTO equity (portfolio_id, date, cash, market_value, total)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(portfolio_id, date) DO UPDATE SET
             cash=excluded.cash, market_value=excluded.market_value, total=excluded.total""",
        (portfolio_id, date, cash, market_value, cash + market_value),
    )
    conn.commit()


def record_trade(
    conn: sqlite3.Connection,
    portfolio_id: str,
    date: str,
    symbol: str,
    side: str,
    shares: int,
    price: float,
    fee: float,
    realized_pnl: float,
    reason: str,
    model_id: str,
) -> None:
    amount = shares * price
    conn.execute(
        """INSERT OR IGNORE INTO trades
           (portfolio_id, date, symbol, side, shares, price, amount, fee,
            realized_pnl, reason, model_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            portfolio_id,
            date,
            symbol,
            side,
            shares,
            price,
            amount,
            fee,
            realized_pnl,
            reason,
            model_id,
        ),
    )


def save_decision(
    conn: sqlite3.Connection,
    model_id: str,
    date: str,
    targets: list,
    market_view: str,
    confidence: float,
    raw: str,
    latency_ms: int,
    prompt_tokens: int,
    completion_tokens: int,
    error: Optional[str] = None,
) -> None:
    import json

    conn.execute(
        """INSERT INTO decisions
           (model_id, date, targets_json, market_view, confidence, raw,
            latency_ms, prompt_tokens, completion_tokens, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(model_id, date) DO UPDATE SET
             targets_json=excluded.targets_json,
             market_view=excluded.market_view,
             confidence=excluded.confidence,
             raw=excluded.raw,
             latency_ms=excluded.latency_ms,
             prompt_tokens=excluded.prompt_tokens,
             completion_tokens=excluded.completion_tokens,
             error=excluded.error""",
        (
            model_id,
            date,
            json.dumps(targets, ensure_ascii=False),
            market_view,
            confidence,
            raw,
            latency_ms,
            prompt_tokens,
            completion_tokens,
            error,
        ),
    )
    conn.commit()


def latest_pending_decision(
    conn: sqlite3.Connection, model_id: str, before_date: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM decisions
           WHERE model_id = ? AND date < ? AND executed_date IS NULL
           ORDER BY date DESC LIMIT 1""",
        (model_id, before_date),
    ).fetchone()


def latest_decision(conn: sqlite3.Connection, model_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM decisions WHERE model_id = ? ORDER BY date DESC LIMIT 1",
        (model_id,),
    ).fetchone()
