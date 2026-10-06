"""Daily scheduled job (15:30 via Cline Schedule / cron).

Runs the full pipeline, appends a row to ``daily_snapshots`` (the accumulating data
asset shown on the dashboard), writes an HTML report to ``reports/``, fires a macOS
banner (success *and* failure, like the Whale project) and dispatches the e-mail /
Lark alert when the churn thresholds are crossed.

Usage
-----
python scripts/daily_run.py            # daily run
python scripts/daily_run.py --dry-run  # skip fetch (fast, reuses raw data)
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import logging
import sqlite3
import sys
import time
import traceback
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, db, notify  # noqa: E402
from scripts.run_pipeline import run_pipeline  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("churn.daily")

RISK_LABELS = {"高频套利者", "噪音/机器人"}


def _query(sql: str, default=None):
    try:
        conn = sqlite3.connect(config.DB_PATH)
        try:
            row = conn.execute(sql).fetchone()
            return row[0] if row else default
        finally:
            conn.close()
    except Exception:
        return default


def _risk_cluster_ratio() -> float:
    total = _query("SELECT COUNT(*) FROM address_clusters", 0) or 0
    if not total:
        return 0.0
    risky = _query(
        "SELECT COUNT(*) FROM address_clusters WHERE cluster_label IN ('高频套利者','噪音/机器人')",
        0) or 0
    return risky / total


def _write_snapshot(summary: dict, risk_ratio: float) -> None:
    conn = db.connect()
    try:
        n_addr = db.table_count(conn, "address_features")
        n_tx = db.table_count(conn, "raw_transactions")
        conn.execute(
            """INSERT OR REPLACE INTO daily_snapshots
               (snapshot_date, n_addresses, n_transactions, churn_rate,
                high_risk_ratio, avg_daily_freq, created_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
            (dt.date.today().isoformat(), n_addr, n_tx,
             float(summary.get("forecast_churn_rate", 0) or 0),
             float(summary.get("high_risk_ratio", 0) or 0),
             float(summary.get("forecast_mean_freq", 0) or 0)),
        )
        conn.commit()
    finally:
        conn.close()


def _write_html(ctx: dict) -> Path:
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.REPORTS_DIR / config.artefact(
        f"daily_report_{ctx['date'].replace('-','')}.html")
    fields = "\n".join(
        f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in ctx["fields"].items())
    badge = "✅ 成功" if ctx["ok"] else "❌ 失败"
    color = "#16a34a" if ctx["ok"] else "#dc2626"
    err = (f"<h3>错误详情</h3><pre>{html.escape(ctx.get('error',''))}</pre>"
           if not ctx["ok"] and ctx.get("error") else "")
    path.write_text(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>加密用户流失预测 每日报告 · {ctx['date']}</title>
<style>body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f4f6fb;padding:32px;}}
.card{{max-width:720px;margin:auto;background:#fff;border-radius:14px;padding:28px;box-shadow:0 6px 24px rgba(15,23,42,.08);}}
h1{{font-size:20px;}} .badge{{display:inline-block;padding:4px 12px;border-radius:999px;color:#fff;background:{color};}}
table{{border-collapse:collapse;width:100%;margin-top:16px;}} th,td{{border:1px solid #e5e7eb;padding:8px 12px;text-align:left;}}
th{{background:#f3f4f6;}} a{{color:#4f46e5;}}</style></head>
<body><div class="card"><h1>加密用户行为聚类 &amp; 流失预测 · 每日报告</h1>
<p>日期：{ctx['date']} ｜ 生成时间：{ctx['generated_at']} ｜ 状态：<span class="badge">{badge}</span></p>
<table>{fields}</table>{err}
<p style="margin-top:18px;"><a href="{config.DASHBOARD_URL}">打开实时看板 →</a></p></div></body></html>""",
                     encoding="utf-8")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="skip the network fetch")
    args = parser.parse_args(argv)

    started = time.time()
    date_str = dt.date.today().isoformat()
    ctx = {"date": date_str, "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "ok": False, "error": "", "fields": {}}
    try:
        results = run_pipeline(skip_fetch=args.dry_run)
        summary = (results.get("churn_model") or (None, None, {}))[2] or {}
        fs = summary.get("forecast_summary", {}) if isinstance(summary, dict) else {}
        risk_ratio = _risk_cluster_ratio()

        conn = db.connect()
        try:
            n_tx = db.table_count(conn, "raw_transactions")
            n_addr = db.table_count(conn, "address_features")
        finally:
            conn.close()
        _write_snapshot(fs, risk_ratio)

        digest = notify.collect_cluster_digest()
        triggered = notify.send_alerts(fs, risk_cluster_ratio=risk_ratio, digest=digest)
        # 群机器人每天都要收到「聚类结果 + 洞察」，不受流失阈值门控。
        lark_digest_ok = notify.send_lark_digest(fs, risk_ratio, digest=digest)

        ctx["ok"] = all(v[0] == "ok" for v in results.values())
        ctx["fields"] = {
            "运行状态": "✅ 成功" if ctx["ok"] else "❌ 部分阶段失败",
            "原始交易数 (raw_transactions)": n_tx,
            "地址数 (address_features)": n_addr,
            "预测流失率 (30天)": f"{fs.get('forecast_churn_rate', 0):.2%}",
            "高危地址占比": f"{fs.get('high_risk_ratio', 0):.2%}",
            "高危行为簇占比": f"{risk_ratio:.2%}",
            "人均频次降低值": fs.get("avg_freq_drop", "-"),
            "预警是否触发": "✅ 已发送邮件/Lark" if triggered else "未触发（低于阈值）",
            "Lark 简报（聚类+洞察）": "✅ 已推送" if lark_digest_ok else "未推送（缺 LARK_WEBHOOK_URL 或发送失败）",
            "看板地址": config.DASHBOARD_URL,
            "运行时长": f"{round(time.time() - started, 1)} 秒",
        }
        report = _write_html(ctx)
        notify.notify_macos(
            f"流失率 {fs.get('forecast_churn_rate', 0):.1%}，高危 {fs.get('high_risk_ratio', 0):.1%}",
            title="✅ 加密流失预测 · 每日运行成功")
        try:
            webbrowser.open(f"file://{report}")
        except Exception:
            pass
        logger.info("每日任务成功 -> %s", report)
        return 0
    except Exception as exc:  # noqa: BLE001
        reason = str(exc).strip() or exc.__class__.__name__
        ctx["ok"] = False
        ctx["error"] = traceback.format_exc()
        ctx["fields"] = {"运行状态": "❌ 失败", "失败原因": reason}
        try:
            _write_html(ctx)
        except Exception:
            pass
        notify.notify_macos(f"每日任务失败：{reason}", title="❌ 加密流失预测 · 运行失败")
        logger.error("每日任务失败: %s", reason)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
