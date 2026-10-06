"""Stage 3 — unsupervised behaviour clustering.

Standardises the feature matrix and clusters addresses with **HDBSCAN** (chosen over
K-Means because on-chain behaviour is dense in the head and sparse in the tail:
HDBSCAN finds variable-density clusters and labels outliers as noise — likely bots
or one-off addresses — instead of forcing them into a cluster).

Each cluster is auto-labelled from its centroid into one of the business personas
(高频大户 / 长期持有者 / DeFi 农民 / 高频套利者). A cluster-distribution pie chart
and a cluster-profile radar chart are written to ``reports/`` and the assignment
(plus 2-D t-SNE coordinates) is stored in ``address_clusters``.

Usage
-----
python src/cluster_analyzer.py
"""
from __future__ import annotations

import logging
import sys
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
logger = logging.getLogger("churn.cluster")

FEATURE_COLS = [
    "tx_freq_daily", "tx_freq_weekly", "tx_freq_monthly", "avg_holding_hours",
    "high_gas_ratio", "avg_gas_price_gwei", "protocol_diversity", "unique_tokens",
    "token_tx_ratio", "eth_balance_end", "eth_balance_trend", "total_tx", "active_days",
]

PERSONAS = ["高频大户", "长期持有者", "DeFi农民", "高频套利者"]
NOISE_LABEL = "噪音/机器人"


def load_features() -> pd.DataFrame:
    conn = db.connect()
    try:
        df = pd.read_sql_query("SELECT * FROM address_features", conn)
    finally:
        conn.close()
    return df


def _standardize(df: pd.DataFrame) -> Tuple[np.ndarray, "object"]:
    from sklearn.preprocessing import StandardScaler

    X = df[FEATURE_COLS].fillna(0).to_numpy(dtype=float)
    scaler = StandardScaler()
    return scaler.fit_transform(X), scaler


def _label_clusters(feats: pd.DataFrame, labels: np.ndarray) -> Dict[int, str]:
    """Assign a business persona to each cluster id based on centroid scores."""
    df = feats.copy()
    df["cluster"] = labels
    # z-score each feature so scores are comparable across scales.
    z = (df[FEATURE_COLS] - df[FEATURE_COLS].mean()) / (df[FEATURE_COLS].std(ddof=0) + 1e-9)
    z["cluster"] = labels

    scores: Dict[int, Dict[str, float]] = {}
    for cid, g in z.groupby("cluster"):
        if cid == -1:
            continue
        scores[int(cid)] = {
            "高频大户": g["tx_freq_daily"].mean() + g["eth_balance_end"].mean(),
            "长期持有者": g["avg_holding_hours"].mean() - g["tx_freq_daily"].mean(),
            "DeFi农民": g["protocol_diversity"].mean() + g["token_tx_ratio"].mean(),
            "高频套利者": g["tx_freq_daily"].mean() + g["high_gas_ratio"].mean()
            + g["protocol_diversity"].mean() - g["avg_holding_hours"].mean(),
        }

    # Greedy assignment of distinct personas to clusters by descending score.
    assignment: Dict[int, str] = {}
    used: set[str] = set()
    ranked = sorted(scores.items(),
                    key=lambda kv: (max(kv[1].values()) if kv[1] else -1e9), reverse=True)
    for cid, persona_scores in ranked:
        best = max(
            (p for p in PERSONAS if p not in used),
            key=lambda p: persona_scores.get(p, -1e9),
            default=None,
        )
        if best is None:
            best = max(persona_scores, key=persona_scores.get)
        assignment[cid] = best
        used.add(best)
    assignment[-1] = NOISE_LABEL
    return assignment


def _tsne_coords(X_scaled: np.ndarray) -> np.ndarray:
    from sklearn.manifold import TSNE

    n = X_scaled.shape[0]
    if n < 5:
        return np.zeros((n, 2))
    perplexity = min(30, max(5, (n - 1) // 3))
    tsne = TSNE(n_components=2, perplexity=perplexity, init="pca",
                learning_rate="auto", random_state=config.RANDOM_STATE)
    return tsne.fit_transform(X_scaled)


def _write_charts(feats: pd.DataFrame, labels: np.ndarray,
                  assignment: Dict[int, str]) -> None:
    import plotly.express as px
    import plotly.graph_objects as go

    df = feats.copy()
    df["cluster"] = labels
    df["label"] = df["cluster"].map(assignment)

    # --- Pie: cluster distribution ---
    counts = df["label"].value_counts().reset_index()
    counts.columns = ["label", "count"]
    pie = px.pie(counts, names="label", values="count", hole=0.35,
                 title="地址行为簇分布 (HDBSCAN)",
                 color_discrete_sequence=px.colors.qualitative.Set3)
    pie.write_image(str(config.REPORTS_DIR / config.artefact("cluster_pie.png")))

    # --- Radar: cluster profiles over normalised key features ---
    radar_feats = ["tx_freq_daily", "avg_holding_hours", "protocol_diversity",
                   "token_tx_ratio", "high_gas_ratio", "eth_balance_end"]
    prof = df.groupby("label")[radar_feats].mean()
    norm = (prof - prof.min()) / (prof.max() - prof.min() + 1e-9)
    fig = go.Figure()
    for label, row in norm.iterrows():
        fig.add_trace(go.Scatterpolar(r=row.to_list() + [row.iloc[0]],
                                      theta=radar_feats + [radar_feats[0]],
                                      fill="toself", name=str(label)))
    fig.update_layout(title="簇画像雷达图 (归一化)", polar=dict(radialaxis=dict(visible=True, range=[0, 1])))
    fig.write_image(str(config.REPORTS_DIR / config.artefact("cluster_radar.png")))
    logger.info("图表已保存到 %s", config.REPORTS_DIR)


def run() -> pd.DataFrame:
    feats = load_features()
    if feats.empty:
        logger.warning("address_features 为空，请先运行 feature_engineer.py")
        return pd.DataFrame()

    X_scaled, _ = _standardize(feats)

    import hdbscan
    n = len(feats)
    min_cluster_size = max(2, min(config.HDBSCAN_MIN_CLUSTER_SIZE, max(2, n // 4)))
    min_samples = max(1, min(config.HDBSCAN_MIN_SAMPLES, min_cluster_size))
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",
    )
    labels = clusterer.fit_predict(X_scaled)

    assignment = _label_clusters(feats, labels)
    coords = _tsne_coords(X_scaled)

    out = pd.DataFrame({
        "address": feats["address"].values,
        "cluster_id": labels,
        "cluster_label": [assignment.get(int(c), NOISE_LABEL) for c in labels],
        "is_noise": [1 if c == -1 else 0 for c in labels],
        "tsne_x": coords[:, 0],
        "tsne_y": coords[:, 1],
    })

    conn = db.connect()
    try:
        out.to_sql("address_clusters", conn, if_exists="replace", index=False)
        db.record_run(conn, "cluster_analyzer", "ok",
                      f"clusters={out['cluster_id'].nunique()}, noise={int(out['is_noise'].sum())}")
    finally:
        conn.close()

    try:
        _write_charts(feats, labels, assignment)
    except Exception as exc:  # chart writing (kaleido) must not break the pipeline
        logger.warning("图表生成失败（可忽略）: %s", exc)

    logger.info("聚类完成：%d 个地址 -> %d 个簇 (含噪音 %d)",
                len(out), out["cluster_id"].nunique(), int(out["is_noise"].sum()))
    return out


def main(argv=None) -> int:
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
