#!/usr/bin/env bash
#
# Bootstrap entrypoint for the churn dashboard container.
#
# Goal: `docker compose up -d --build` (or the one-shot start.sh helper) on a fresh
# clone should end with a populated dashboard at http://localhost:8501, with the
# whole pipeline already executed. It does this in two steps:
#
#   1. Run  — if the database has no feature rows yet, execute the full pipeline
#             (fetch -> features -> clusters -> churn) respecting ADDRESS_LIMIT.
#             A marker file makes this a one-time job; later boots skip straight to
#             the dashboard.
#   2. Serve— exec the container CMD (Streamlit) in the foreground.
#
set -euo pipefail

cd /app
mkdir -p /app/data /app/reports
DB_PATH="${CHURN_DB_PATH:-/app/data/crypto_churn.db}"
MARKER="/app/data/.pipeline_done"

feature_count() {
  python - <<'PY' 2>/dev/null || echo 0
import sqlite3, os
p = os.environ.get("CHURN_DB_PATH", "/app/data/crypto_churn.db")
try:
    c = sqlite3.connect(p)
    print(int(c.execute("SELECT COUNT(*) FROM address_features").fetchone()[0]))
except Exception:
    print(0)
PY
}

COUNT="$(feature_count)"
if [ ! -f "$MARKER" ] || [ "${FORCE_PIPELINE:-0}" = "1" ]; then
  if [ "${COUNT:-0}" -lt 2 ]; then
    echo "[entrypoint] 首次启动：运行完整流水线 (ADDRESS_LIMIT=${ADDRESS_LIMIT:-200})…"
    if [ -n "${ETHERSCAN_API_KEY:-}" ]; then
      if python scripts/run_pipeline.py; then
        touch "$MARKER"
      else
        echo "[entrypoint] 流水线部分失败（API 配额/网络），仍启动看板以便查看已有数据"
      fi
    else
      echo "[entrypoint] 未配置 ETHERSCAN_API_KEY，跳过抓取；请在 environment.env 中设置后重建"
    fi
  else
    echo "[entrypoint] 已存在特征数据 (${COUNT} 个地址)，跳过流水线"
    touch "$MARKER"
  fi
else
  echo "[entrypoint] 流水线已在之前的启动中完成，直接启动看板"
fi

echo "[entrypoint] 启动：$*"
exec "$@"
