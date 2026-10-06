"""Unit tests for the crypto churn pipeline (no network, no heavy models).

Run:  python -m pytest -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import churn_model, feature_engineer, notify  # noqa: E402


def _sample_transactions() -> pd.DataFrame:
    base = 1_700_000_000
    rows = []
    addr = "0xaaa"
    for i in range(10):
        rows.append({
            "address": addr, "tx_hash": f"h{i}", "timestamp": base + i * 86400,
            "from_address": addr if i % 2 else "0xbbb",
            "to_address": "0xbbb" if i % 2 else addr,
            "value_eth": 1.0, "tx_type": "normal", "contract_address": "",
            "token_symbol": "ETH", "gas_used": 21000, "gas_price": 50e9, "is_error": 0,
        })
    # an ERC-20 receive + later send to exercise the holding-time logic
    rows.append({
        "address": addr, "tx_hash": "e1", "timestamp": base,
        "from_address": "0xccc", "to_address": addr, "value_eth": 100.0,
        "tx_type": "erc20", "contract_address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "token_symbol": "USDT", "gas_used": 50000, "gas_price": 40e9, "is_error": 0,
    })
    rows.append({
        "address": addr, "tx_hash": "e2", "timestamp": base + 2 * 86400,
        "from_address": addr, "to_address": "0xccc", "value_eth": 100.0,
        "tx_type": "erc20", "contract_address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "token_symbol": "USDT", "gas_used": 50000, "gas_price": 40e9, "is_error": 0,
    })
    return pd.DataFrame(rows)


def test_feature_engineer_produces_all_columns():
    feats = feature_engineer.compute_features(_sample_transactions())
    assert len(feats) == 1
    for col in feature_engineer_test_columns():
        assert col in feats.columns
    row = feats.iloc[0]
    assert row["total_tx"] == 12
    assert row["tx_freq_daily"] > 0
    assert row["protocol_diversity"] >= 1
    # received at base, sent 2 days later -> ~48h holding
    assert 47 <= row["avg_holding_hours"] <= 49


def feature_engineer_test_columns():
    return [
        "tx_freq_daily", "tx_freq_weekly", "tx_freq_monthly", "avg_holding_hours",
        "high_gas_ratio", "protocol_diversity", "unique_tokens", "token_tx_ratio",
        "eth_balance_end", "eth_balance_trend", "last_tx_days_ago",
    ]


def test_churn_label_uses_threshold():
    df = pd.DataFrame({
        "last_tx_days_ago": [1.0, 31.0, 60.0, 10.0],
        "tx_freq_daily": [1, 0, 0, 2],
    })
    # pad feature columns so build_xy works
    for col in churn_model.MODEL_FEATURES:
        if col not in df:
            df[col] = 0.0
    X, y = churn_model.build_xy(df)
    assert list(y) == [0, 1, 1, 0]


def test_alert_thresholds():
    summary = {"forecast_churn_rate": 0.55, "high_risk_ratio": 0.30, "avg_freq_drop": 1.2}
    triggered, reasons, ctx = notify.evaluate_alerts(summary)
    assert triggered is True
    assert any("流失率" in r for r in reasons)
    assert ctx["churn_rate"].endswith("%")


def test_alert_not_triggered_below_threshold():
    summary = {"forecast_churn_rate": 0.05, "high_risk_ratio": 0.02, "avg_freq_drop": 0.1}
    triggered, _, _ = notify.evaluate_alerts(summary)
    assert triggered is False


def test_email_template_renders_without_leftover_placeholders():
    _, _, ctx = notify.evaluate_alerts(
        {"forecast_churn_rate": 0.4, "high_risk_ratio": 0.2, "avg_freq_drop": 0.5})
    html = notify.render_email(ctx)
    assert "{{" not in html
    assert "40.0%" in html


def test_frequency_vs_churn_buckets():
    df = pd.DataFrame({
        "tx_freq_daily": [0.0, 0.0, 2.0, 3.0, 0.5],
        "last_tx_days_ago": [100, 90, 1, 2, 40],
    })
    agg = churn_model.analyze_frequency_vs_churn(df)
    assert set(agg["freq_bucket"].astype(str)).issuperset({"~0 (≈静止)"})
    assert agg["churn_rate"].max() > 0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
