"""Stage 5 — Streamlit dashboard (16 panels).

A rich, single-page board that reads straight from SQLite (``crypto_churn[_test].db``)
plus the JSON hand-off written by ``src/churn_model.py``. Panels:

 1. 核心指标卡片         2. 簇分布饼图            3. 簇画像雷达图
 4. t-SNE 聚类散点图      5. 模型评估指标表         6. 三模型对比柱状图
 7. 簇 × 流失率           8. 流失概率分布           9. SHAP 全局重要性
10. SHAP 单样本解释      11. 频次区间 × 流失率     12. 未来 30 天预测双线
13. 高危地址 TOP 表       14. 累积数据资产(快照)     15. 管道运行健康
16. 业务洞察文字区

Run:  streamlit run app/dashboard.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402

st.set_page_config(page_title="加密用户行为聚类 & 流失预测", page_icon="🪙", layout="wide")


@st.cache_data(ttl=60)
def load_df(table: str) -> pd.DataFrame:
    try:
        conn = sqlite3.connect(config.DB_PATH)
        try:
            return pd.read_sql_query(f"SELECT * FROM {table}", conn)
        finally:
            conn.close()
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_summary() -> dict:
    path = config.REPORTS_DIR / config.artefact("churn_summary.json")
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


# --- load everything ----------------------------------------------------------
feats = load_df("address_features")
clusters = load_df("address_clusters")
churn = load_df("churn_predictions")
forecast = load_df("churn_forecast")
daily = load_df("forecast_daily")
metrics = load_df("model_metrics")
snapshots = load_df("daily_snapshots")
runs = load_df("pipeline_runs")
raw = load_df("raw_transactions")
summary = load_summary()
fs = summary.get("forecast_summary", {})

# --- header -------------------------------------------------------------------
st.title("🪙 加密货币用户行为聚类分析与流失预测")
mode = "🧪 测试模式 (200 地址)" if config.TEST_MODE else "🚀 全量模式 (2000 地址)"
st.caption(f"{mode} ｜ 数据库 `{config.DB_PATH}` ｜ 参考时间基准=数据集最新时间戳")

with st.sidebar:
    st.header("⚙️ 控制台")
    st.write(f"地址上限：`{config.ADDRESS_LIMIT}`")
    st.write(f"流失判定：`last_tx_days_ago > {config.CHURN_DAYS}` 天")
    st.write(f"预警阈值：流失率 ≥ `{config.ALERT_CHURN_RATE_THRESHOLD:.0%}`")
    st.write(f"高危簇阈值：`{config.ALERT_RISK_CLUSTER_RATIO:.0%}`")
    if st.button("🔄 刷新缓存"):
        st.cache_data.clear()
        st.rerun()
    st.divider()
    st.write("**数据资产**")
    st.write(f"原始交易：`{len(raw):,}`")
    st.write(f"覆盖地址：`{feats['address'].nunique() if not feats.empty else 0}`")
    st.write(f"聚类簇数：`{clusters['cluster_id'].nunique() if not clusters.empty else 0}`")

# --- Panel 1: metric cards ----------------------------------------------------
st.subheader("1️⃣ 核心指标")
c = st.columns(6)
n_addr = feats["address"].nunique() if not feats.empty else 0
churn_rate = float(fs.get("forecast_churn_rate", 0) or 0)
high_risk = float(fs.get("high_risk_ratio", 0) or 0)
c[0].metric("监控地址数", f"{n_addr:,}")
c[1].metric("原始交易数", f"{len(raw):,}")
c[2].metric("已流失地址 (实际)", f"{int(churn['is_churned'].sum()) if not churn.empty else 0}")
c[3].metric("预测流失率 (30天)", f"{churn_rate:.1%}")
c[4].metric("高危地址占比", f"{high_risk:.1%}")
top_feat = next(iter(summary.get("shap_importance", {})), "-")
c[5].metric("SHAP 首要特征", top_feat)

# --- Panel 2 & 3: cluster distribution + radar --------------------------------
col2, col3 = st.columns(2)
with col2:
    st.subheader("2️⃣ 簇分布")
    if not clusters.empty:
        cnt = clusters["cluster_label"].value_counts().reset_index()
        cnt.columns = ["label", "count"]
        fig = px.pie(cnt, names="label", values="count", hole=0.4,
                     color_discrete_sequence=px.colors.qualitative.Set3)
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("暂无聚类结果，请先运行 cluster_analyzer.py")

with col3:
    st.subheader("3️⃣ 簇画像雷达图 (归一化)")
    if not feats.empty and not clusters.empty:
        merged = feats.merge(clusters[["address", "cluster_label"]], on="address", how="left")
        rf = ["tx_freq_daily", "avg_holding_hours", "protocol_diversity",
              "token_tx_ratio", "high_gas_ratio", "eth_balance_end"]
        prof = merged.groupby("cluster_label")[rf].mean()
        norm = (prof - prof.min()) / (prof.max() - prof.min() + 1e-9)
        fig = go.Figure()
        for label, row in norm.iterrows():
            fig.add_trace(go.Scatterpolar(r=row.to_list() + [row.iloc[0]],
                                          theta=rf + [rf[0]], fill="toself", name=str(label)))
        fig.update_layout(height=420, polar=dict(radialaxis=dict(range=[0, 1])),
                          margin=dict(t=30, b=10))
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("暂无数据")

# --- Panel 4: t-SNE scatter ---------------------------------------------------
st.subheader("4️⃣ t-SNE 聚类散点图")
if not clusters.empty and {"tsne_x", "tsne_y"}.issubset(clusters.columns):
    fig = px.scatter(clusters, x="tsne_x", y="tsne_y", color="cluster_label",
                     hover_data=["address"], opacity=0.75,
                     color_discrete_sequence=px.colors.qualitative.Bold)
    fig.update_layout(height=460, legend_title="簇标签")
    st.plotly_chart(fig, width="stretch")
else:
    st.info("暂无 t-SNE 坐标，请先运行 cluster_analyzer.py")

# --- Panel 5 & 6: model metrics + comparison ----------------------------------
col5, col6 = st.columns([1, 1])
with col5:
    st.subheader("5️⃣ 模型评估指标")
    if not metrics.empty:
        show = metrics.rename(columns={"model_name": "模型"})
        st.dataframe(show.style.format({
            "auc": "{:.3f}", "f1": "{:.3f}", "precision_": "{:.3f}", "recall": "{:.3f}",
            "accuracy": "{:.3f}"}), width="stretch")
    else:
        st.info("暂无模型指标，请先运行 churn_model.py")
with col6:
    st.subheader("6️⃣ 三模型对比 (AUC / F1)")
    if not metrics.empty:
        m = metrics.melt(id_vars="model_name", value_vars=["auc", "f1"],
                         var_name="指标", value_name="值")
        fig = px.bar(m, x="model_name", y="值", color="指标", barmode="group",
                     color_discrete_sequence=px.colors.qualitative.Set1)
        fig.update_layout(height=380, xaxis_title="模型")
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("暂无数据")

# --- Panel 7 & 8: cluster x churn + prob distribution -------------------------
col7, col8 = st.columns(2)
with col7:
    st.subheader("7️⃣ 各簇流失率")
    if not clusters.empty and not churn.empty:
        m = clusters.merge(churn, on="address", how="left")
        agg = m.groupby("cluster_label").agg(
            n=("address", "size"), churn_rate=("is_churned", "mean")).reset_index()
        fig = px.bar(agg, x="cluster_label", y="churn_rate", text="n",
                     color="churn_rate", color_continuous_scale="Reds")
        fig.update_layout(height=380, xaxis_title="簇", yaxis_title="实际流失率")
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("暂无数据")
with col8:
    st.subheader("8️⃣ 流失概率分布")
    if not churn.empty and "churn_prob" in churn.columns:
        fig = px.histogram(churn.dropna(subset=["churn_prob"]), x="churn_prob", nbins=20,
                           color="is_churned", color_discrete_sequence=["#22c55e", "#ef4444"])
        fig.update_layout(height=380, xaxis_title="预测流失概率", yaxis_title="地址数")
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("暂无数据")

# --- Panel 9 & 10: SHAP -------------------------------------------------------
col9, col10 = st.columns(2)
with col9:
    st.subheader("9️⃣ SHAP 全局特征重要性")
    imp = summary.get("shap_importance", {})
    if imp:
        imp_df = pd.DataFrame({"feature": list(imp), "importance": list(imp.values())})
        imp_df = imp_df.sort_values("importance").tail(12)
        fig = px.bar(imp_df, x="importance", y="feature", orientation="h",
                     color="importance", color_continuous_scale="Blues")
        fig.update_layout(height=420, yaxis_title="")
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("暂无 SHAP 结果")
with col10:
    st.subheader("🔟 SHAP 单样本解释 (最高风险地址)")
    single = summary.get("shap_single_sample", {})
    if single:
        sdf = pd.DataFrame({"feature": list(single), "shap": list(single.values())})
        sdf["contrast"] = sdf["shap"] > 0
        sdf = sdf.reindex(sdf["shap"].abs().sort_values(ascending=False).index).head(10)
        fig = px.bar(sdf, x="shap", y="feature", orientation="h", color="contrast",
                     color_discrete_map={True: "#ef4444", False: "#22c55e"})
        fig.update_layout(height=420, showlegend=False, yaxis_title="")
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("暂无单样本 SHAP")

# --- Panel 11 & 12: frequency vs churn + 30d forecast -------------------------
col11, col12 = st.columns(2)
with col11:
    st.subheader("1️⃣1️⃣ 交易频次区间 × 流失率")
    fvc = summary.get("freq_vs_churn", [])
    if fvc:
        fdf = pd.DataFrame(fvc)
        fig = px.bar(fdf, x="freq_bucket", y="churn_rate", text="n",
                     color="churn_rate", color_continuous_scale="OrRd")
        fig.update_layout(height=400, xaxis_title="日交易频次区间", yaxis_title="实际流失率")
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("暂无数据")

with col12:
    st.subheader("1️⃣2️⃣ 未来 30 天：人均频次 & 流失率预测")
    if not daily.empty:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=daily["day_index"], y=daily["avg_daily_freq"],
                                 name="人均日交易频次", yaxis="y1", mode="lines+markers"))
        fig.add_trace(go.Scatter(x=daily["day_index"], y=daily["churn_rate"],
                                 name="预测流失率", yaxis="y2", mode="lines+markers"))
        fig.update_layout(
            height=400, xaxis_title="未来天数",
            yaxis=dict(title="人均日交易频次"),
            yaxis2=dict(title="流失率", overlaying="y", side="right", tickformat=".0%"))
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("暂无预测数据")

# --- Panel 13: high-risk TOP table --------------------------------------------
st.subheader("1️⃣3️⃣ 高危地址 TOP 20")
if not forecast.empty:
    top = forecast.sort_values("forecast_churn_prob", ascending=False).head(20)
    st.dataframe(top, width="stretch", height=360)
else:
    st.info("暂无预测数据")

# --- Panel 14 & 15: data asset + pipeline health ------------------------------
col14, col15 = st.columns(2)
with col14:
    st.subheader("1️⃣4️⃣ 累积数据资产 (每日快照)")
    if not snapshots.empty:
        snapshots = snapshots.sort_values("snapshot_date")
        fig = go.Figure()
        fig.add_trace(go.Bar(x=snapshots["snapshot_date"], y=snapshots["n_transactions"],
                             name="交易数", yaxis="y1"))
        fig.add_trace(go.Scatter(x=snapshots["snapshot_date"], y=snapshots["churn_rate"],
                                 name="流失率", yaxis="y2", mode="lines+markers"))
        fig.update_layout(height=380, xaxis_title="日期",
                          yaxis=dict(title="交易数"),
                          yaxis2=dict(title="流失率", overlaying="y", side="right",
                                      tickformat=".0%"))
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("尚无每日快照，运行 scripts/daily_run.py 后累积")
with col15:
    st.subheader("1️⃣5️⃣ 管道运行健康")
    if not runs.empty:
        r = runs.sort_values("ran_at", ascending=False).head(20)
        st.dataframe(r, width="stretch", height=360)
    else:
        st.info("暂无运行记录")

# --- Panel 16: business insight text ------------------------------------------
st.subheader("1️⃣6️⃣ 业务洞察")
insight_path = config.DOCS_DIR / "insights.md"
if insight_path.exists():
    st.markdown(insight_path.read_text(encoding="utf-8"))
else:
    st.info("暂无洞察文本，请先运行 churn_model.py")

st.caption("© 加密货币用户行为聚类分析与流失预测 · 数据来源 Etherscan V2 · "
           "聚类 HDBSCAN · 分类 LightGBM/XGBoost/RandomForest · 解释 SHAP · 预测 ARIMA")
