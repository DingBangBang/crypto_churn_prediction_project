"""Unit tests for the crypto churn pipeline (no network, no heavy models).

Run:  python -m pytest -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

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


def _temp_cluster_db(path: Path) -> Path:
    """Minimal SQLite fixture holding exactly the columns the Lark digest queries."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE address_clusters (address TEXT, cluster_id INTEGER, cluster_label TEXT);
        CREATE TABLE address_features (address TEXT, tx_freq_daily REAL, avg_holding_hours REAL,
            protocol_diversity REAL, high_gas_ratio REAL, eth_balance_end REAL,
            last_tx_days_ago REAL);
        CREATE TABLE churn_predictions (address TEXT, is_churned INTEGER, churn_prob REAL);
    """)
    rows = [
        ("a1", "高频大户", 4.0, 8.0, 3.0, 0.0, 10.0, 70.0, 1),
        ("a2", "高频大户", 2.0, 9.0, 4.0, 0.0, 20.0, 80.0, 0),
        ("a3", "长期持有者", 1.0, 30.0, 5.0, 0.3, -9.0, 12.0, 0),
        ("a4", "高频套利者", 0.4, 28.0, 2.6, 0.9, -6.0, 71.0, 1),
        ("a5", "噪音/机器人", 67.0, 895.0, 73.0, 0.3, 8578.0, 36.0, 0),
    ]
    for addr, label, freq, hold, proto, gas, bal, idle, churned in rows:
        conn.execute("INSERT INTO address_clusters VALUES (?, ?, ?)", (addr, 0, label))
        conn.execute("INSERT INTO address_features VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (addr, freq, hold, proto, gas, bal, idle))
        conn.execute("INSERT INTO churn_predictions VALUES (?, ?, ?)", (addr, churned, 0.5))
    conn.commit()
    conn.close()
    return path


def test_cluster_digest_aggregates_sqlite(tmp_path):
    digest = notify.collect_cluster_digest(_temp_cluster_db(tmp_path / "t.db"))
    assert digest["n_addresses"] == 5
    assert digest["n_personas"] == 3            # 噪音不是 persona
    assert digest["n_noise"] == 1
    assert digest["risk_ratio"] == pytest.approx(2 / 5)   # 噪音 + 高频套利者
    text = notify.render_cluster_text(digest)
    assert "高频大户" in text and "实际流失率 50.0%" in text
    assert "噪音/机器人" in text


def test_cluster_digest_survives_missing_db(tmp_path):
    digest = notify.collect_cluster_digest(tmp_path / "nope.db")
    assert digest["rows"] == [] and digest["n_addresses"] == 0
    assert "暂无可用的聚类数据" in notify.render_cluster_text(digest)


def test_digest_text_carries_warning_clusters_and_insights():
    digest = {"rows": [{"label": "长期持有者", "n": 4, "pct": 1.0, "freq": 1.0, "holding": 30.0,
                        "protocols": 5.0, "gas_ratio": 0.1, "balance": -9.0, "idle_days": 12.0,
                        "churn_rate": 0.1}],
              "n_addresses": 4, "n_personas": 1, "n_noise": 0, "risk_ratio": 0.0}
    text = notify.build_digest_text(
        {"forecast_churn_rate": 0.51, "high_risk_ratio": 0.67, "avg_freq_drop": 14.5,
         "current_mean_freq": 33.8, "final_day_freq": 19.2, "horizon": 30},
        0.0, digest=digest, insights="· 结论\n建议对高危簇提前触达。")
    assert "每日简报" in text
    assert "一、流失预警" in text and "预测流失率（30 天）: 51.0%" in text
    assert "二、【用户行为聚类结果】" in text and "长期持有者" in text
    assert "三、业务洞察" in text and "建议对高危簇提前触达" in text
    assert "http://localhost:8501" in text


def test_lark_payload_only_mentions_a_real_open_id(monkeypatch):
    monkeypatch.setattr(notify.config, "LARK_AT_ID", "13339947334")   # 手机号无法 @
    assert "<at" not in notify._lark_payload("hi")["content"]["text"]
    monkeypatch.setattr(notify.config, "LARK_AT_ID", "ou_abc123")
    payload = notify._lark_payload("hi")
    assert payload["content"]["text"].startswith('<at user_id="ou_abc123"></at>')


def test_read_insights_dedupes_and_strips_noise(tmp_path, monkeypatch):
    (tmp_path / "insights.md").write_text(
        "# 业务洞察 (Insights)\n\n> 本文件由 `src/churn_model.py` 自动生成/更新。\n\n"
        "## 流失预测结论 (自动生成)\n\n### 驱动流失的关键特征 (SHAP)\n"
        "- **total_tx**: 平均 |SHAP| = 1.14\n\n### 结论\n第 30 天预计流失率 51.5%。\n\n"
        "---\n\n### 驱动流失的关键特征 (SHAP)\n- **total_tx**: 平均 |SHAP| = 9.99\n",
        encoding="utf-8")
    monkeypatch.setattr(notify.config, "DOCS_DIR", tmp_path)
    text = notify.read_insights()
    assert "1.14" in text                      # 保留第一份（最新）正文
    assert "9.99" not in text                  # 丢弃历史重复块
    assert "本文件由" not in text and "**" not in text
    assert "结论" in text


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
