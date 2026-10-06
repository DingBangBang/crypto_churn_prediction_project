"""四个去向的统一分发层（邮件 / Lark 卡片 / macOS 通知 / 运行状态页）。

为什么要单独一层
----------------
流水线本身（``scripts/run_pipeline.py``：抓取 → 特征 → 聚类 → 预测）与「把结果送出去」
是两件事，且**两种入口都要用到后者**：

* ``scripts/daily_run.py`` —— 定时任务 / 本机跑，需要快照、报告、四去向、自动打开页面；
* ``docker-entrypoint.sh`` → ``scripts/run_pipeline.py --deliver`` —— 容器首启一键跑完，
  同样要发邮件/Lark、生成状态页（容器里只是不能弹 macOS 通知、不能打开浏览器）。

所以这里提供 ``deliver()`` / ``deliver_failure()`` 两个函数，**谁跑完流水线谁调用**，
避免「compose 只发告警、没有状态页」这种两套口径。

用法
----
::

    python -m src.deliver                  # 读 churn_summary.json -> 发四去向
    python -m src.deliver --no-send        # 只生成报告/状态页，不发网络请求（自检）
    python -m src.deliver --no-open        # 不自动打开页面（容器 / CI）
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import os
import sqlite3
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict

from src import config, db, notify, status_page

logger = logging.getLogger("churn.deliver")

# 高危行为簇：这两个簇的占比超过阈值（ALERT_RISK_CLUSTER_RATIO）即视为风险集中。
RISK_LABELS = ("高频套利者", "噪音/机器人")


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


def risk_cluster_ratio() -> float:
    """高危行为簇（高频套利 / 噪音机器人）在全部地址中的占比。"""
    total = _query("SELECT COUNT(*) FROM address_clusters", 0) or 0
    if not total:
        return 0.0
    labels = ",".join(f"'{label}'" for label in RISK_LABELS)
    risky = _query(
        f"SELECT COUNT(*) FROM address_clusters WHERE cluster_label IN ({labels})", 0) or 0
    return risky / total


def load_forecast_summary() -> Dict[str, Any]:
    """从 ``reports/<tag>churn_summary.json`` 读回预测口径（④ 阶段的 JSON 交接文件）。

    容器首启时 ``run_pipeline.py`` 已经算过一遍并落盘，所以这里**不重算**，直接复用，
    保证「预测 → 四去向」口径完全一致。
    """
    path = config.REPORTS_DIR / config.artefact("churn_summary.json")
    if not path.exists():
        logger.warning("未找到 %s（请先跑完 ④ 流失预测）", path)
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("forecast_summary", {}) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("解析 %s 失败: %s", path, exc)
        return {}


def in_container() -> bool:
    return Path("/.dockerenv").exists() or os.getenv("CHURN_HEADLESS") == "1"


def can_open_browser() -> bool:
    """容器里没有 GUI（``open``/``webbrowser`` 都会静默失败），只在 macOS 宿主上打开。"""
    return sys.platform == "darwin" and not in_container()


def notify_desktop(title: str, message: str, page: Path | None) -> None:
    """macOS 通知（点按直达状态页）；容器内只记日志，不报错。"""
    if in_container():
        logger.info("容器内无 GUI，跳过 macOS 通知；状态页在 %s", page or "-")
        return
    notify.notify_macos(message, title=title, url=str(page) if page else None)


def open_page(page: Path | None) -> None:
    """打开运行状态页（macOS 上用 LaunchServices，见 ``notify.open_browser``）。"""
    if not page or not can_open_browser():
        return
    notify.open_browser(str(page))


def write_snapshot(summary: Dict[str, Any], risk_ratio: float) -> None:
    """把当日结果写入 ``daily_snapshots``（看板第 15 面板的数据资产）。"""
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


def write_run_meta(date_str: str, ok: bool, elapsed_s: float, error: str = "",
                   report: Path | None = None) -> Path:
    """写 ``reports/last_run.json`` —— 简报卡片与状态页共用的「本次运行总体结论」。"""
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.REPORTS_DIR / "last_run.json"
    path.write_text(json.dumps({
        "date": date_str,
        "ok": bool(ok),
        "elapsed_s": elapsed_s,
        "error": error or "",
        "report": str(report) if report else "",
        "finished_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_html(ctx: Dict[str, Any]) -> Path:
    """当日运行报告（``reports/daily_report_<date>[_test].html``，邮件/状态页都会链接它）。"""
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.REPORTS_DIR / config.artefact(
        f"daily_report_{ctx['date'].replace('-', '')}.html")
    fields = "\n".join(
        f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in ctx["fields"].items())
    badge = "✅ 成功" if ctx["ok"] else "❌ 失败"
    color = "#16a34a" if ctx["ok"] else "#dc2626"
    err = (f"<h3>错误详情</h3><pre>{html.escape(ctx.get('error', ''))}</pre>"
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




def deliver(forecast_summary: Dict[str, Any] | None = None, *, started: float | None = None,
            results: Dict[str, Any] | None = None, ok: bool | None = None,
            risk_ratio: float | None = None, send: bool = True,
            open_when_done: bool = True) -> Dict[str, Any]:
    """把一次运行的结果送到四个去向（邮件 / Lark / 状态页 / 通知）+ 落盘快照与报告。

    ``forecast_summary`` 缺省时从 ``churn_summary.json`` 读回（容器里已有该文件 → 不重算）。
    ``results`` 是 ``run_pipeline`` 的返回值，用来判断「每个阶段成功/失败」；不传则视为成功。
    """
    started = started if started is not None else time.time()
    date_str = dt.date.today().isoformat()
    fs = dict(forecast_summary) if forecast_summary else load_forecast_summary()
    risk_ratio = risk_cluster_ratio() if risk_ratio is None else risk_ratio
    if ok is None:
        ok = all(v[0] == "ok" for v in results.values()) if results else True

    ctx: Dict[str, Any] = {"date": date_str,
                           "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           "ok": bool(ok), "error": "", "fields": {}}

    conn = db.connect()
    try:
        n_tx = db.table_count(conn, "raw_transactions")
        n_addr = db.table_count(conn, "address_features")
    finally:
        conn.close()
    write_snapshot(fs, risk_ratio)

    digest = notify.collect_cluster_digest()
    status = notify.collect_run_status()
    status["ok"] = bool(ok)
    status["elapsed_s"] = round(time.time() - started, 1)
    if send:
        email_ok = notify.send_alerts(fs, risk_cluster_ratio=risk_ratio,
                                      digest=digest, status=status)
        # 群机器人每天都要收到「聚类结果 + 洞察」，不受流失阈值门控。
        lark_ok = notify.send_lark_digest(fs, risk_ratio, digest=digest, status=status)
    else:
        email_ok = lark_ok = False
        logger.info("--no-send：跳过邮件 / Lark 发送，仅生成报告与运行状态页")

    risk = notify.risk_level(fs.get("forecast_churn_rate", 0) or 0)
    ctx["fields"] = {
        "运行状态": "✅ 成功" if ctx["ok"] else "❌ 部分阶段失败",
        "原始交易数 (raw_transactions)": n_tx,
        "地址数 (address_features)": n_addr,
        "风险等级": f"{risk['label']}（{risk['multiple']}× 阈值，"
                    f"{'必须重视' if risk['must_act'] else '暂不强制处置'}）",
        "预测流失率 (30天)": f"{fs.get('forecast_churn_rate', 0):.2%}",
        "多时间窗流失率": " ｜ ".join(
            f"{int(r['horizon'])}d {float(r.get('churn_rate') or 0):.1%}"
            for r in notify.horizon_rows(fs)) or "-",
        "高危地址占比": f"{fs.get('high_risk_ratio', 0):.2%}",
        "高危行为簇占比": f"{risk_ratio:.2%}",
        "人均频次降低值": fs.get("avg_freq_drop", "-"),
        "预警是否触发": ("✅ 已发送邮件/Lark" if email_ok
                     else ("未触发（低于阈值）" if send else "已跳过（--no-send）")),
        "Lark 简报（聚类+洞察）": ("✅ 已推送" if lark_ok
                              else ("未推送（缺 LARK_WEBHOOK_URL 或发送失败）" if send
                                    else "已跳过（--no-send）")),
        "看板地址": config.DASHBOARD_URL,
        "运行时长": f"{round(time.time() - started, 1)} 秒",
    }
    report = write_html(ctx)
    write_run_meta(date_str, ok=bool(ctx["ok"]), elapsed_s=round(time.time() - started, 1),
                   report=report)
    page = status_page.build_status_page(
        fs, digest=digest, status=status, email_ok=email_ok, lark_ok=lark_ok,
        report_path=report, n_transactions=n_tx, n_addresses=n_addr)
    notify_desktop(
        f"运行成功 ｜ 流失率 {fs.get('forecast_churn_rate', 0):.1%}"
        f" ｜ {risk['label']} ｜ 点开看 邮件/Lark/看板",
        "✅ 加密流失预测 · 每日运行成功", page)
    if open_when_done:
        open_page(page)
    logger.info("四去向分发完成：邮件=%s Lark=%s 状态页=%s", email_ok, lark_ok, page)
    return {"ok": bool(ctx["ok"]), "report": report, "status_page": page,
            "email_sent": bool(email_ok), "lark_sent": bool(lark_ok), "risk": risk,
            "n_transactions": n_tx, "n_addresses": n_addr,
            "elapsed_s": round(time.time() - started, 1)}


def deliver_failure(reason: str, *, started: float | None = None, error_tb: str = "",
                    open_when_done: bool = True) -> Dict[str, Any]:
    """失败也要交付：报告 + 状态页（含报错原文）+ 快照，一个都不能少。"""
    started = started if started is not None else time.time()
    date_str = dt.date.today().isoformat()
    ctx: Dict[str, Any] = {"date": date_str,
                           "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           "ok": False, "error": error_tb, "fields": {}}
    ctx["fields"] = {"运行状态": "❌ 失败", "失败原因": reason}
    report = None
    try:
        report = write_html(ctx)
    except Exception:  # pragma: no cover - 报告写不出来也要继续
        pass
    page = None
    try:
        status = notify.collect_run_status()
        status["ok"] = False
        status["error"] = reason
        status["elapsed_s"] = round(time.time() - started, 1)
        page = status_page.build_status_page(status=status, report_path=report)
    except Exception as exc:  # noqa: BLE001
        logger.warning("生成运行状态页失败: %s", exc)
    write_run_meta(date_str, ok=False, elapsed_s=round(time.time() - started, 1), error=reason)
    notify_desktop(f"运行失败：{reason} ｜ 点开看 邮件/Lark/看板/报错详情",
                   "❌ 加密流失预测 · 运行失败", page)
    if open_when_done:
        open_page(page)
    logger.error("运行失败已交付: %s", reason)
    return {"ok": False, "report": report, "status_page": page, "error": reason}


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="四个去向统一分发：读 churn_summary.json -> 邮件/Lark/状态页/通知")
    parser.add_argument("--no-send", action="store_true",
                        help="不发邮件/Lark（只生成报告与状态页，用于自检）")
    parser.add_argument("--no-open", action="store_true", help="不自动打开页面")
    parser.add_argument("--simulate-failure", metavar="REASON",
                        help="自检用：直接走失败路径（验证失败状态页）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    started = time.time()
    if args.simulate_failure:
        out = deliver_failure(args.simulate_failure, started=started,
                              error_tb="(模拟失败：仅用于验证失败状态页)",
                              open_when_done=not args.no_open)
        return 1 if out.get("status_page") else 1
    if not load_forecast_summary():
        logger.error("没有可用的预测结果（%s 缺失），请先跑 scripts/run_pipeline.py",
                     config.REPORTS_DIR / config.artefact("churn_summary.json"))
        return 2
    out = deliver(started=started, send=not args.no_send, open_when_done=not args.no_open)
    logger.info("完成 | 邮箱=%s Lark=%s 状态页=%s", out["email_sent"], out["lark_sent"],
                out["status_page"])
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
