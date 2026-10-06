"""Stage 2 — feature engineering.

Turns the raw transaction table into a per-address behavioural feature matrix and
writes it to ``address_features``. Six feature families are computed:

1. 交易频次      -> ``tx_freq_daily`` / ``tx_freq_weekly`` / ``tx_freq_monthly``
2. 平均持仓时长  -> ``avg_holding_hours`` (token received -> later transferred out)
3. Gas 消耗模式  -> ``high_gas_ratio`` / ``avg_gas_price_gwei``
4. 协议多样性    -> ``protocol_diversity`` (distinct contracts interacted with)
5. 代币行为      -> ``unique_tokens`` / ``token_tx_ratio``
6. 资产规模变化  -> ``eth_balance_start`` / ``eth_balance_end`` / ``eth_balance_trend``

The analysis reference time ("now") is the newest timestamp in the dataset so the
labels stay meaningful no matter when the offline pipeline is executed.

Usage
-----
python src/feature_engineer.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("churn.feature_engineer")

DAY = 86400.0
GWEI = 1e9


def load_transactions() -> pd.DataFrame:
    conn = db.connect()
    try:
        df = pd.read_sql_query(
            "SELECT address, tx_hash, timestamp, from_address, to_address, value_eth, "
            "tx_type, contract_address, token_symbol, gas_used, gas_price, is_error "
            "FROM raw_transactions",
            conn,
        )
    finally:
        conn.close()
    if not df.empty:
        df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce").fillna(0).astype("int64")
    return df


def _holding_hours(group: pd.DataFrame) -> float:
    """Average hours between *receiving* a token and later *sending* it (FIFO)."""
    token = group[group["tx_type"] == "erc20"].sort_values("timestamp")
    if token.empty:
        return 0.0
    holdings: Dict[str, List[List[float]]] = {}
    durations: List[float] = []
    for _, row in token.iterrows():
        contract = row["contract_address"] or "unknown"
        amt = float(row["value_eth"] or 0)
        ts = int(row["timestamp"])
        addr = row["address"]
        if row["to_address"] == addr and row["from_address"] != addr:
            holdings.setdefault(contract, []).append([ts, amt])
        elif row["from_address"] == addr and row["to_address"] != addr:
            queue = holdings.get(contract, [])
            remaining = amt
            while remaining > 0 and queue:
                recv_ts, recv_amt = queue[0]
                used = min(recv_amt, remaining)
                if used > 0:
                    durations.append((ts - recv_ts) / 3600.0)
                queue[0][1] -= used
                remaining -= used
                if queue[0][1] <= 1e-12:
                    queue.pop(0)
    return float(np.mean(durations)) if durations else 0.0


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    now_ref = int(df["timestamp"].max())
    total_gas = pd.to_numeric(df["gas_price"], errors="coerce").fillna(0)
    # "High gas" = above the dataset's 75th percentile gas price (dynamic baseline).
    high_gas_threshold = float(total_gas[total_gas > 0].quantile(0.75)) if (total_gas > 0).any() else 0.0

    records: List[Dict[str, object]] = []
    for addr, g in df.groupby("address"):
        g = g.sort_values("timestamp")
        n = len(g)
        first_ts = int(g["timestamp"].min())
        last_ts = int(g["timestamp"].max())
        active_days = max((last_ts - first_ts) / DAY, 1.0)

        freq_daily = n / active_days
        freq_weekly = freq_daily * 7
        freq_monthly = freq_daily * 30

        # --- Gas ---
        gp = pd.to_numeric(g["gas_price"], errors="coerce").fillna(0)
        nonzero = gp[gp > 0]
        avg_gas_gwei = float(nonzero.mean()) / GWEI if len(nonzero) else 0.0
        high_gas_ratio = float((nonzero > high_gas_threshold).mean()) if len(nonzero) else 0.0

        # --- Protocol diversity: distinct contracts (erc20 tokens + zero-value calls) ---
        contracts = set()
        erc = g[g["tx_type"] == "erc20"]
        contracts |= set(erc["contract_address"].dropna().unique()) - {"", "unknown"}
        eth_calls = g[(g["tx_type"].isin(["normal", "internal"])) & (g["value_eth"] == 0)]
        contracts |= {c for c in eth_calls["to_address"].dropna().unique() if c}
        protocol_diversity = len(contracts)

        # --- Token behaviour ---
        unique_tokens = int(erc["contract_address"].replace("", np.nan).dropna().nunique())
        token_tx_ratio = len(erc) / n if n else 0.0

        # --- ETH balance change (cumulative net flow + slope) ---
        eth = g[g["tx_type"].isin(["normal", "internal"])].copy()
        eth["net"] = np.where(eth["to_address"] == addr, eth["value_eth"], -eth["value_eth"])
        cum = eth["net"].cumsum().to_numpy(dtype=float)
        eth_balance_end = float(cum[-1]) if len(cum) else 0.0
        if len(cum) >= 2:
            x = (eth["timestamp"].to_numpy(dtype=float) - first_ts) / DAY
            slope = np.polyfit(x, cum, 1)[0] if np.ptp(x) > 0 else 0.0
        else:
            slope = 0.0
        eth_balance_trend = float(slope)  # ETH per day (signed)

        last_tx_days_ago = (now_ref - last_ts) / DAY
        active_day_count = g["timestamp"].apply(lambda t: int(t // DAY)).nunique()

        records.append({
            "address": addr,
            "tx_freq_daily": round(freq_daily, 4),
            "tx_freq_weekly": round(freq_weekly, 4),
            "tx_freq_monthly": round(freq_monthly, 4),
            "avg_holding_hours": round(_holding_hours(g), 2),
            "high_gas_ratio": round(high_gas_ratio, 4),
            "avg_gas_price_gwei": round(avg_gas_gwei, 3),
            "protocol_diversity": protocol_diversity,
            "unique_tokens": unique_tokens,
            "token_tx_ratio": round(token_tx_ratio, 4),
            "eth_balance_start": 0.0,
            "eth_balance_end": round(eth_balance_end, 6),
            "eth_balance_trend": round(eth_balance_trend, 6),
            "total_tx": n,
            "last_tx_days_ago": round(last_tx_days_ago, 2),
            "active_days": int(active_day_count),
        })
    return pd.DataFrame(records)


def write_features(feats: pd.DataFrame) -> int:
    if feats.empty:
        logger.warning("No features to write (empty transaction table?)")
        return 0
    conn = db.connect()
    try:
        feats.to_sql("address_features", conn, if_exists="replace", index=False)
        db.record_run(conn, "feature_engineer", "ok", f"addresses={len(feats)}")
    finally:
        conn.close()
    return len(feats)


def main(argv=None) -> int:
    df = load_transactions()
    logger.info("读取原始交易 %d 行，覆盖 %d 个地址",
                len(df), df["address"].nunique() if not df.empty else 0)
    feats = compute_features(df)
    n = write_features(feats)
    logger.info("特征工程完成：%d 个地址 -> address_features 表", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
