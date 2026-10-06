"""Shared SQLite schema + helper for the crypto churn project.

Every pipeline stage reads/writes the same SQLite file (``data/crypto_churn[_test].db``)
so the stages can be run independently and re-run idempotently. Tables are created
with ``CREATE TABLE IF NOT EXISTS`` and writes use ``INSERT OR REPLACE`` / a UNIQUE
key so re-running a stage never duplicates rows.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402

SCHEMA = """
-- Raw on-chain transactions (normal + internal + erc20) for each watched address.
CREATE TABLE IF NOT EXISTS raw_transactions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    address          TEXT NOT NULL,          -- the watched address this row belongs to
    tx_hash          TEXT NOT NULL,
    block_number     INTEGER,
    timestamp        INTEGER,                -- unix seconds
    from_address     TEXT,
    to_address       TEXT,
    value_eth        REAL DEFAULT 0,
    value_usd        REAL DEFAULT 0,
    tx_type          TEXT,                   -- normal | internal | erc20
    contract_address TEXT,                   -- token contract for erc20 rows
    token_symbol     TEXT,
    gas_used         INTEGER DEFAULT 0,
    gas_price        REAL DEFAULT 0,         -- wei
    is_error         INTEGER DEFAULT 0,
    fetched_at       TEXT DEFAULT (datetime('now')),
    UNIQUE(address, tx_hash, tx_type)
);
CREATE INDEX IF NOT EXISTS idx_raw_address ON raw_transactions(address);
CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_transactions(timestamp);

-- Per-address behavioural feature matrix (stage 2 output).
CREATE TABLE IF NOT EXISTS address_features (
    address             TEXT PRIMARY KEY,
    tx_freq_daily       REAL,
    tx_freq_weekly      REAL,
    tx_freq_monthly     REAL,
    avg_holding_hours   REAL,
    high_gas_ratio      REAL,
    avg_gas_price_gwei  REAL,
    protocol_diversity  INTEGER,
    unique_tokens       INTEGER,
    token_tx_ratio      REAL,
    eth_balance_start   REAL,
    eth_balance_end     REAL,
    eth_balance_trend   REAL,
    total_tx            INTEGER,
    last_tx_days_ago    REAL,
    active_days         INTEGER,
    computed_at         TEXT DEFAULT (datetime('now'))
);

-- Stage 3 clustering output.
CREATE TABLE IF NOT EXISTS address_clusters (
    address       TEXT PRIMARY KEY,
    cluster_id    INTEGER,
    cluster_label TEXT,
    is_noise      INTEGER,
    tsne_x        REAL,
    tsne_y        REAL,
    clustered_at  TEXT DEFAULT (datetime('now'))
);

-- Stage 4 churn model output (per address).
CREATE TABLE IF NOT EXISTS churn_predictions (
    address      TEXT PRIMARY KEY,
    churn_label  INTEGER,
    churn_prob   REAL,
    is_churned   INTEGER,
    predicted_at TEXT DEFAULT (datetime('now'))
);

-- Stage 4 model-evaluation metrics (one row per model).
CREATE TABLE IF NOT EXISTS model_metrics (
    model_name  TEXT PRIMARY KEY,
    auc         REAL,
    f1          REAL,
    precision_  REAL,
    recall      REAL,
    accuracy    REAL,
    n_train     INTEGER,
    n_test      INTEGER,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Stage 4 ARIMA forecast: per-address projected transaction frequency.
CREATE TABLE IF NOT EXISTS churn_forecast (
    address            TEXT PRIMARY KEY,
    current_daily_freq REAL,
    forecast_daily_freq REAL,     -- predicted avg daily freq over next 30 days
    freq_drop          REAL,      -- current - forecast
    forecast_churn_prob REAL,     -- model prob re-scored on the forecast features
    risk_band          TEXT,      -- low | medium | high
    forecast_at        TEXT DEFAULT (datetime('now'))
);

-- Stage 4 daily forecast roll-up (per future day) for the dashboard.
CREATE TABLE IF NOT EXISTS forecast_daily (
    day_index     INTEGER PRIMARY KEY,  -- 1..30
    avg_daily_freq REAL,
    churn_rate    REAL,
    freq_drop     REAL
);

-- Stage 9: accumulating data-asset snapshots written by the daily scheduler.
CREATE TABLE IF NOT EXISTS daily_snapshots (
    snapshot_date    TEXT PRIMARY KEY,   -- YYYY-MM-DD
    n_addresses      INTEGER,
    n_transactions   INTEGER,
    churn_rate       REAL,
    high_risk_ratio  REAL,
    avg_daily_freq   REAL,
    created_at       TEXT DEFAULT (datetime('now'))
);

-- Pipeline run bookkeeping (used by the scheduler + dashboard health panel).
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    stage      TEXT,
    status     TEXT,
    detail     TEXT,
    ran_at     TEXT DEFAULT (datetime('now'))
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open (and initialise) the project SQLite database."""
    db_path = path or config.DB_PATH
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_run(conn: sqlite3.Connection, stage: str, status: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO pipeline_runs (stage, status, detail) VALUES (?, ?, ?)",
        (stage, status, detail),
    )
    conn.commit()


def table_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return 0
