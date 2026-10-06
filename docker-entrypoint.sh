#!/usr/bin/env bash
#
# Bootstrap entrypoint for the churn dashboard container.
#
# Goal: `docker compose up -d --build` (or the one-shot start.sh helper) on a fresh
# clone should end with a populated dashboard at http://localhost:8501, *and* with the
# four outputs already delivered on the host:
#
#   ① 邮件     reports/…（收件人见 ALERT_EMAIL_TO）      -> 需要 Gmail 凭据
#   ② Lark 卡片 群机器人（阈值告警 + 每日聚类/洞察简报）  -> 需要 LARK_WEBHOOK_URL
#   ③ 运行状态页 reports/status.html                    -> 报错内容 + 三入口 + 结果 + 建议
#   ④ macOS 通知 只在宿主机做（容器无 GUI，start.sh 负责弹/打开）
#
# It does this in three steps:
#
#   1. Run   — if the database has no feature rows yet, execute the full pipeline
#              (fetch -> features -> clusters -> churn) respecting ADDRESS_LIMIT,
#              then hand the result to the four-channel delivery (--deliver).
#              A marker file makes this a one-time job; later boots skip straight to
#              the dashboard.
#   2. Deliver — `python scripts/daily_run.py --deliver-only` is *not* needed when the
#              pipeline ran here: `--deliver` already did it in the same process
#              (identical code path, see src/deliver.py).
#   3. Serve — exec the container CMD (Streamlit) in the foreground.
#
set -euo pipefail

cd /app
mkdir -p /app/data /app/reports
DB_PATH="${CHURN_DB_PATH:-/app/data/crypto_churn.db}"
MARKER="/app/data/.pipeline_done"

echo "============================================================"
echo " 🐳 容器启动：① 检查数据库 → ② 跑流水线（带进度条）"
echo "              → ③ 交付四去向（邮件/Lark/状态页）→ ④ 启动看板"
echo " 看不到进度时，另开一个终端：docker compose logs -f churn-app"
echo "============================================================"

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

# 容器内不开 GUI：确认交付层不会尝试弹通知/开浏览器（幂等，环境变量优先）。
export CHURN_HEADLESS="${CHURN_HEADLESS:-1}"

DELIVER_FLAG=""
if [ "${RUN_DELIVER:-1}" = "1" ]; then
  DELIVER_FLAG="--deliver"
  echo "[entrypoint] 四去向交付已开启（邮件/Lark/状态页/通知）"
else
  echo "[entrypoint] RUN_DELIVER=0：只跑流水线，不发邮件/Lark、不生成状态页"
fi

COUNT="$(feature_count)"
if [ ! -f "$MARKER" ] || [ "${FORCE_PIPELINE:-0}" = "1" ]; then
  if [ "${COUNT:-0}" -lt 2 ]; then
    echo "[entrypoint] 首次启动：运行完整流水线 (ADDRESS_LIMIT=${ADDRESS_LIMIT:-200})${DELIVER_FLAG:+ + 四去向}…"
    if [ -n "${ETHERSCAN_API_KEY:-}" ]; then
      if python scripts/run_pipeline.py ${DELIVER_FLAG}; then
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
    if [ -n "${DELIVER_FLAG}" ]; then
      echo "[entrypoint] 复用已有结果补发四去向：python scripts/daily_run.py --deliver-only"
      python scripts/daily_run.py --deliver-only --no-open || \
        echo "[entrypoint] 交付失败（网络/凭据），不影响看板启动"
    fi
  fi
else
  echo "[entrypoint] 流水线已在之前的启动中完成，直接启动看板"
fi

if [ -f /app/reports/status.html ]; then
  echo "[entrypoint] 运行状态页（宿主机路径）：./reports/status.html"
fi
echo "[entrypoint] 启动：$*"
exec "$@"
