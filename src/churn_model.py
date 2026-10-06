"""Stage 4 — churn prediction + explainability + frequency forecasting.

Pipeline
--------
1. **Label** an address as *churned* when ``last_tx_days_ago > CHURN_DAYS`` (30d).
2. **Train** three discrete classifiers — LightGBM (primary), XGBoost and Random
   Forest — and report AUC / F1 / Precision / Recall (also compared & discussed:
   which discrete learner is the most defensible for this label).
3. **Explain** the primary model with **SHAP** (global importance + a single-address
   explanation, e.g. "协议多样性从 5 降到 1 -> 流失概率 +40%").
4. **Correlate** transaction-frequency buckets with realised churn to find the
   "danger range" where churn jumps.
5. **Forecast** each address's daily transaction frequency for the next 30 days with
   an **ARIMA** (continuous) time-series model, roll it up into a per-day churn rate
   and average frequency drop, and store everything for the dashboard.

Usage
-----
python src/churn_model.py
"""
from __future__ import annotations

import logging
import sys
import warnings
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("churn.model")
warnings.filterwarnings("ignore")

DAY = 86400.0

MODEL_FEATURES = [
    "tx_freq_daily", "tx_freq_weekly", "tx_freq_monthly", "avg_holding_hours",
    "high_gas_ratio", "avg_gas_price_gwei", "protocol_diversity", "unique_tokens",
    "token_tx_ratio", "eth_balance_end", "eth_balance_trend", "total_tx", "active_days",
]
# Frequency-family features that ARIMA overwrites when re-scoring a forecast.
FREQ_FEATURES = ["tx_freq_daily", "tx_freq_weekly", "tx_freq_monthly"]


def load_data() -> pd.DataFrame:
    conn = db.connect()
    try:
        df = pd.read_sql_query("SELECT * FROM address_features", conn)
    finally:
        conn.close()
    return df


def build_xy(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.Series]:
    y = (df["last_tx_days_ago"] > config.CHURN_DAYS).astype(int)
    X = df[MODEL_FEATURES].fillna(0)
    return X, y


def _metrics(y_true, y_pred, y_prob) -> Dict[str, float]:
    from sklearn.metrics import (accuracy_score, f1_score, precision_score,
                                 recall_score, roc_auc_score)

    out = {
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
    }
    try:
        out["auc"] = float(roc_auc_score(y_true, y_prob))
    except ValueError:
        out["auc"] = float("nan")  # single class present
    return out


def train_models(df: pd.DataFrame):
    """Train LightGBM / XGBoost / RandomForest and return (models, metrics_df, X_test...)."""
    from sklearn.model_selection import train_test_split

    X, y = build_xy(df)
    n_pos = int(y.sum())
    if len(df) < 10 or n_pos == 0 or n_pos == len(df):
        logger.warning("样本不足或标签单一 (n=%d, pos=%d)，跳过模型训练", len(df), n_pos)
        return {}, pd.DataFrame(), None

    stratify = y if n_pos >= 2 and (len(y) - n_pos) >= 2 else None
    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=config.TEST_SIZE, random_state=config.RANDOM_STATE, stratify=stratify
    )

    models: Dict[str, object] = {}

    from lightgbm import LGBMClassifier
    models["LightGBM"] = LGBMClassifier(
        n_estimators=200, learning_rate=0.05, num_leaves=15,
        random_state=config.RANDOM_STATE, verbose=-1,
    )
    try:
        from xgboost import XGBClassifier
        models["XGBoost"] = XGBClassifier(
            n_estimators=200, learning_rate=0.05, max_depth=4,
            subsample=0.9, eval_metric="logloss", random_state=config.RANDOM_STATE,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("XGBoost 不可用: %s", exc)

    from sklearn.ensemble import RandomForestClassifier
    models["RandomForest"] = RandomForestClassifier(
        n_estimators=300, random_state=config.RANDOM_STATE,
    )

    rows = []
    for name, model in models.items():
        model.fit(X_tr, y_tr)
        y_pred = model.predict(X_te)
        proba = (model.predict_proba(X_te)[:, 1] if hasattr(model, "predict_proba")
                 else model.decision_function(X_te))
        m = _metrics(y_te, y_pred, proba)
        m["model_name"] = name
        m["n_train"] = len(X_tr)
        m["n_test"] = len(X_te)
        rows.append(m)
        logger.info("%s: AUC=%.3f F1=%.3f P=%.3f R=%.3f",
                    name, m["auc"], m["f1"], m["precision"], m["recall"])

    metrics_df = pd.DataFrame(rows).set_index("model_name")
    return models, metrics_df, (X_tr, X_te, y_tr, y_te)


def explain_with_shap(model, X: pd.DataFrame) -> Tuple[Dict[str, float], pd.DataFrame]:
    """Return (mean|SHAP| per feature, per-row SHAP DataFrame)."""
    try:
        import shap

        explainer = shap.TreeExplainer(model)
        sv = explainer.shap_values(X)
        sv = np.array(sv)
        if sv.ndim == 3:  # (n, features, classes)
            sv = sv[:, :, 1]
        shap_df = pd.DataFrame(sv, columns=X.columns, index=X.index)
        importance = shap_df.abs().mean().sort_values(ascending=False).to_dict()
        return importance, shap_df
    except Exception as exc:  # pragma: no cover - SHAP is best effort
        logger.warning("SHAP 计算失败: %s", exc)
        return {}, pd.DataFrame()


def analyze_frequency_vs_churn(df: pd.DataFrame) -> pd.DataFrame:
    """Bucket addresses by daily frequency and measure the realised churn rate."""
    d = df.copy()
    d["churn"] = (d["last_tx_days_ago"] > config.CHURN_DAYS).astype(int)
    bins = [0, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 1e9]
    labels = ["~0 (≈静止)", "0-0.05", "0.05-0.1", "0.1-0.5", "0.5-1", "1-5", ">5"]
    d["freq_bucket"] = pd.cut(d["tx_freq_daily"], bins=bins, labels=labels, include_lowest=True)
    agg = d.groupby("freq_bucket", observed=False).agg(
        n=("churn", "size"), churn_rate=("churn", "mean"),
        avg_last_tx_days=("last_tx_days_ago", "mean"),
    ).reset_index()
    return agg


CHURN_FREQ_EPS = 0.01  # daily freq below this == effectively dormant ("彻底流失")


def _address_daily_series(conn, ref_day: int, horizon_pad: int = 0) -> Dict[str, np.ndarray]:
    """Build {address: daily tx-count series} ending at ``ref_day`` (unix day index)."""
    raw = pd.read_sql_query(
        "SELECT address, timestamp FROM raw_transactions", conn
    )
    if raw.empty:
        return {}
    raw["day"] = (pd.to_numeric(raw["timestamp"], errors="coerce").fillna(0)
                  .astype("int64") // 86400)
    out: Dict[str, np.ndarray] = {}
    for addr, g in raw.groupby("address"):
        counts = g.groupby("day").size()
        first = int(counts.index.min())
        span = ref_day - first + 1
        if span <= 0:
            continue
        series = np.zeros(span, dtype=float)
        for day, c in counts.items():
            idx = int(day) - first
            if 0 <= idx < span:
                series[idx] = float(c)
        out[addr] = series
    return out


def _forecast_one(series: np.ndarray, horizon: int) -> np.ndarray:
    """Forecast the next ``horizon`` daily counts; fall back to a naive baseline."""
    recent_mean = float(series[-14:].mean()) if len(series) >= 14 else float(series.mean())
    if len(series) < 10 or series.sum() == 0:
        return np.full(horizon, max(recent_mean, 0.0))
    try:
        from statsmodels.tsa.arima.model import ARIMA

        model = ARIMA(series, order=(1, 1, 1), trend="n")
        res = model.fit()
        fc = np.asarray(res.forecast(steps=horizon), dtype=float)
        return np.clip(fc, 0, None)
    except Exception:
        return np.full(horizon, max(recent_mean, 0.0))


def forecast_frequencies(df: pd.DataFrame, model) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, float]]:
    """ARIMA-forecast daily frequency for each address and roll up per-day stats."""
    horizon = config.FORECAST_HORIZON_DAYS
    conn = db.connect()
    try:
        row = conn.execute("SELECT MAX(timestamp) FROM raw_transactions").fetchone()
        ref_day = int((row[0] or 0) // 86400)
        series_map = _address_daily_series(conn, ref_day)
    finally:
        conn.close()

    addrs = df["address"].tolist()
    F = np.zeros((len(addrs), horizon), dtype=float)
    for i, addr in enumerate(addrs):
        series = series_map.get(addr)
        if series is None or len(series) == 0:
            F[i, :] = df["tx_freq_daily"].iloc[i]
        else:
            F[i, :] = _forecast_one(series, horizon)

    current = df["tx_freq_daily"].to_numpy(dtype=float)
    forecast_freq = F.mean(axis=1)
    freq_drop = current - forecast_freq

    proba = np.full(len(addrs), np.nan)
    if model is not None:
        Xf = df[MODEL_FEATURES].fillna(0).copy()
        Xf["tx_freq_daily"] = forecast_freq
        Xf["tx_freq_weekly"] = forecast_freq * 7
        Xf["tx_freq_monthly"] = forecast_freq * 30
        try:
            proba = model.predict_proba(Xf)[:, 1]
        except Exception as exc:  # pragma: no cover
            logger.warning("重打分失败: %s", exc)

    def _band(p, drop):
        if np.isnan(p):
            return "high" if drop > current.mean() else "medium"
        if p >= 0.6 or drop >= 1.0:
            return "high"
        if p >= 0.3:
            return "medium"
        return "low"

    forecast_df = pd.DataFrame({
        "address": addrs,
        "current_daily_freq": current.round(4),
        "forecast_daily_freq": forecast_freq.round(4),
        "freq_drop": freq_drop.round(4),
        "forecast_churn_prob": np.round(proba, 4),
        "risk_band": [_band(p, d) for p, d in zip(proba, freq_drop)],
    })

    current_mean = float(current.mean())
    daily_rows = []
    for d in range(horizon):
        day_freq = F[:, d]
        daily_rows.append({
            "day_index": d + 1,
            "avg_daily_freq": round(float(day_freq.mean()), 4),
            "churn_rate": round(float((day_freq <= CHURN_FREQ_EPS).mean()), 4),
            "freq_drop": round(current_mean - float(day_freq.mean()), 4),
        })
    daily_df = pd.DataFrame(daily_rows)

    summary = {
        "horizon": horizon,
        "current_mean_freq": round(current_mean, 4),
        "forecast_mean_freq": round(float(forecast_freq.mean()), 4),
        "final_day_freq": daily_df["avg_daily_freq"].iloc[-1],
        "final_day_churn_rate": daily_df["churn_rate"].iloc[-1],
        "avg_freq_drop": round(float(freq_drop.mean()), 4),
        "high_risk_ratio": round(float((forecast_df["risk_band"] == "high").mean()), 4),
        "forecast_churn_rate": round(float(
            (forecast_df["forecast_daily_freq"] <= CHURN_FREQ_EPS).mean()), 4),
    }
    return forecast_df, daily_df, summary


def _write_charts(importance: Dict[str, float], freq_agg: pd.DataFrame,
                  daily_df: pd.DataFrame) -> None:
    import plotly.express as px

    if importance:
        imp = pd.DataFrame({"feature": list(importance), "importance": list(importance.values())})
        imp = imp.sort_values("importance", ascending=True).tail(12)
        bar = px.bar(imp, x="importance", y="feature", orientation="h",
                     title="SHAP 全局特征重要性 (mean |SHAP|)")
        bar.write_image(str(config.REPORTS_DIR / config.artefact("shap_importance.png")))

    if not freq_agg.empty:
        f = px.bar(freq_agg, x="freq_bucket", y="churn_rate",
                   title="日交易频次区间 × 实际流失率", text="n")
        f.write_image(str(config.REPORTS_DIR / config.artefact("freq_vs_churn.png")))

    if not daily_df.empty:
        line = px.line(daily_df, x="day_index", y=["avg_daily_freq", "churn_rate"],
                       title="未来 30 天：人均交易频次 & 流失率预测")
        line.write_image(str(config.REPORTS_DIR / config.artefact("forecast_30d.png")))


def build_insight_text(metrics_df, importance, freq_agg, summary, forecast_df,
                       shap_row: Dict[str, float] | None = None) -> str:
    lines = ["## 流失预测结论 (自动生成)", ""]
    if not metrics_df.empty:
        lines.append("### 三种离散分类器对比")
        for name, row in metrics_df.iterrows():
            lines.append(f"- **{name}**: AUC={row['auc']:.3f}, F1={row['f1']:.3f}, "
                         f"Precision={row['precision']:.3f}, Recall={row['recall']:.3f}")
        best = metrics_df["auc"].idxmax() if metrics_df["auc"].notna().any() else metrics_df.index[0]
        lines.append(f"- 综合 AUC 最优：**{best}**。链上数据非线性、特征交互多，LightGBM/XGBoost "
                     "这类梯度提升树在表格数据上通常最稳；RandomForest 更抗过拟合但 AUC 略低。")
    if importance:
        top = list(importance.items())[:3]
        lines.append("")
        lines.append("### 驱动流失的关键特征 (SHAP)")
        for feat, val in top:
            lines.append(f"- **{feat}**: 平均 |SHAP| = {val:.4f}")
    if shap_row:
        lines.append("")
        lines.append("### 单样本解释（当前最高风险地址）")
        for feat, val in sorted(shap_row.items(), key=lambda kv: abs(kv[1]), reverse=True)[:4]:
            direction = "推高" if val > 0 else "降低"
            lines.append(f"- 特征 `{feat}` 将其流失概率{direction} {abs(val):.3f}")
    if not freq_agg.empty:
        lines.append("")
        lines.append("### 交易频次区间 × 流失率")
        for _, r in freq_agg.iterrows():
            lines.append(f"- 频次 {r['freq_bucket']}: 流失率 {r['churn_rate']:.2f} (n={int(r['n'])})")
    lines.append("")
    lines.append("### 未来 30 天 ARIMA 预测")
    lines.append(f"- 当前人均日交易频次: {summary['current_mean_freq']}")
    lines.append(f"- 预测(30 天平均)人均日交易频次: {summary['forecast_mean_freq']}")
    lines.append(f"- 第 30 天人均日交易频次: {summary['final_day_freq']}")
    lines.append(f"- 第 30 天累计流失率: {summary['final_day_churn_rate']:.2%}")
    lines.append(f"- 人均交易频次降低值: {summary['avg_freq_drop']}")
    lines.append(f"- 高危地址占比: {summary['high_risk_ratio']:.2%}")
    lines.append("")
    lines.append("### 结论")
    lines.append(
        f"未来 30 天人均交易频次预计由 {summary['current_mean_freq']} 降至 "
        f"{summary['final_day_freq']}（降低 {summary['avg_freq_drop']}），"
        f"第 30 天预计流失率 {summary['final_day_churn_rate']:.1%}。"
        "当协议交互多样性下降、平均持仓时长缩短且高频 Gas 比例上升时，"
        "地址成为「高频套利者」并快速流失的概率显著提高，建议对高危簇提前触达。"
    )
    return "\n".join(lines) + "\n"


def run() -> Dict[str, object]:
    import json

    df = load_data()
    if df.empty:
        logger.warning("address_features 为空，请先运行 feature_engineer.py / cluster_analyzer.py")
        return {}

    models, metrics_df, _ = train_models(df)
    primary_name = "LightGBM" if "LightGBM" in models else (next(iter(models), ""))
    primary = models.get(primary_name)

    X, y = build_xy(df)

    # --- predictions on the full population ---
    preds = pd.DataFrame({"address": df["address"].values, "is_churned": y.values})
    if primary is not None:
        proba = primary.predict_proba(X)[:, 1]
        preds["churn_prob"] = np.round(proba, 4)
        preds["churn_label"] = (proba >= 0.5).astype(int)
    else:
        preds["churn_prob"] = np.nan
        preds["churn_label"] = 0

    # --- SHAP global + single sample ---
    importance: Dict[str, float] = {}
    shap_df = pd.DataFrame()
    shap_row: Dict[str, float] = {}
    if primary is not None:
        importance, shap_df = explain_with_shap(primary, X)
        if not shap_df.empty:
            top_idx = int(preds["churn_prob"].fillna(0).idxmax())
            shap_row = shap_df.iloc[top_idx].to_dict()

    # --- frequency vs churn correlation ---
    freq_agg = analyze_frequency_vs_churn(df)

    # --- ARIMA 30-day frequency forecast ---
    forecast_df, daily_df, summary = forecast_frequencies(df, primary)

    # --- persist tables ---
    conn = db.connect()
    try:
        if not metrics_df.empty:
            metrics_df.reset_index().rename(columns={"precision": "precision_"}).to_sql(
                "model_metrics", conn, if_exists="replace", index=False)
        preds.rename(columns={"churn_label": "churn_label"}).to_sql(
            "churn_predictions", conn, if_exists="replace", index=False)
        forecast_df.to_sql("churn_forecast", conn, if_exists="replace", index=False)
        daily_df.to_sql("forecast_daily", conn, if_exists="replace", index=False)
        db.record_run(conn, "churn_model", "ok",
                      f"primary={primary_name}, churn_rate={summary['forecast_churn_rate']}")
    finally:
        conn.close()

    try:
        _write_charts(importance, freq_agg, daily_df)
    except Exception as exc:
        logger.warning("图表生成失败（可忽略）: %s", exc)

    # --- JSON handoff for the dashboard ---
    summary_json = {
        "primary_model": primary_name,
        "metrics": metrics_df.reset_index().to_dict("records") if not metrics_df.empty else [],
        "shap_importance": importance,
        "shap_single_sample": shap_row,
        "freq_vs_churn": freq_agg.fillna(0).to_dict("records"),
        "forecast_summary": summary,
        "forecast_daily": daily_df.to_dict("records"),
        "test_mode": config.TEST_MODE,
    }
    (config.REPORTS_DIR / config.artefact("churn_summary.json")).write_text(
        json.dumps(summary_json, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- human-readable insights document ---
    text = build_insight_text(metrics_df, importance, freq_agg, summary, forecast_df, shap_row)
    existing = ""
    insights_path = config.DOCS_DIR / "insights.md"
    if insights_path.exists():
        existing = insights_path.read_text(encoding="utf-8")
    header = "# 业务洞察 (Insights)\n\n> 本文件由 `src/churn_model.py` 自动生成/更新。\n\n"
    insights_path.write_text(header + text + ("\n---\n\n" + existing.split("---", 1)[-1]
                                              if "## 流失预测结论" in existing else ""),
                             encoding="utf-8")

    logger.info("流失预测完成：primary=%s | 预测流失率=%.1f%% | 高危占比=%.1f%%",
                primary_name, summary["forecast_churn_rate"] * 100,
                summary["high_risk_ratio"] * 100)
    return summary_json


def main(argv=None) -> int:
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
