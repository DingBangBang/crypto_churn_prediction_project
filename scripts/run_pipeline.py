"""End-to-end pipeline runner: fetch -> features -> clusters -> churn model.

This is the single entry-point used by ``docker-entrypoint.sh`` and by anyone who
wants to reproduce the whole result with one command. Each stage is imported from
``src`` and run in order; a failing stage is logged but does not abort later stages
so the dashboard always has as much data as possible.

Two delivery modes:

* default  — legacy behaviour: only the *threshold-triggered* e-mail / Lark alert;
* ``--deliver`` — full four-channel delivery (e-mail + Lark card + run-status page +
  macOS notification) via ``src/deliver.py``. This is what ``docker-entrypoint.sh``
  uses, so ``docker compose up`` produces exactly the same outputs as the local
  ``scripts/daily_run.py``.

Usage
-----
python scripts/run_pipeline.py                # full run (respects ADDRESS_LIMIT)
python scripts/run_pipeline.py --skip-fetch   # reuse existing raw data
python scripts/run_pipeline.py --deliver      # ... and push all four channels (container default)
python scripts/run_pipeline.py --deliver --no-send --no-open   # dry self-check, no network
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import churn_model, cluster_analyzer, config, data_fetcher, db, progress  # noqa: E402
from src import deliver, feature_engineer, notify  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("churn.pipeline")


def _stage(name, fn, results, index=None, total=None, title=None):
    """跑一个阶段并记录结果；``index``/``total`` 给出时打印带进度的阶段行。"""
    label = title or name
    started = progress.stage(index, total, label) if index else time.time()
    t0 = time.time()
    try:
        out = fn()
        results[name] = ("ok", round(time.time() - t0, 1), out)
        if index:
            progress.stage_done(index, total, label, started)
        logger.info("✅ %s 完成 (%.1fs)", name, time.time() - t0)
    except Exception as exc:  # noqa: BLE001
        results[name] = ("fail", round(time.time() - t0, 1), str(exc))
        logger.error("❌ %s 失败: %s", name, exc)
        if index:
            progress.stage_done(index, total, f"{label}（失败）", started)
    return results


def run_pipeline(skip_fetch: bool = False, total_stages: int = 4) -> dict:
    """按顺序跑 ①抓取 → ②特征 → ③聚类 → ④预测（``--deliver`` 时 total_stages=5）。"""
    results: dict = {}
    if not skip_fetch:
        _stage("data_fetcher", data_fetcher.fetch_population, results,
               index=1, total=total_stages, title="抓取链上交易（Etherscan）")
    else:
        logger.info("[1/%d] ⏭ 跳过 data_fetcher（--skip-fetch：复用已有原始数据）", total_stages)

    _stage("feature_engineer",
           lambda: feature_engineer.write_features(feature_engineer.compute_features(
               feature_engineer.load_transactions())),
           results, index=2, total=total_stages, title="特征工程（六大类特征）")
    _stage("cluster_analyzer", cluster_analyzer.run, results,
           index=3, total=total_stages, title="行为聚类（HDBSCAN）")
    _stage("churn_model", churn_model.run, results,
           index=4, total=total_stages, title="流失预测（三模型 + SHAP + ARIMA）")
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the full crypto churn pipeline.")
    parser.add_argument("--skip-fetch", action="store_true", help="reuse existing raw data")
    parser.add_argument("--no-alert", action="store_true", help="do not send alerts")
    parser.add_argument("--deliver", action="store_true",
                        help="跑完走四去向：邮件 + Lark 卡片 + 运行状态页 + macOS 通知"
                             "（docker compose 首启即用这个；与本机 daily_run 同一份 deliver()）")
    parser.add_argument("--no-send", action="store_true",
                        help="配合 --deliver：不发邮件/Lark，只生成报告与状态页（自检）")
    parser.add_argument("--no-open", action="store_true",
                        help="配合 --deliver：不自动打开状态页（容器/CI）")
    args = parser.parse_args(argv)

    started = time.time()
    total_stages = 5 if args.deliver else 4
    progress.banner(
        "🪙 加密货币用户行为聚类分析与流失预测 · 流水线\n"
        "步骤：" + " → ".join([
            "① 抓取链上交易", "② 特征工程", "③ 行为聚类", "④ 流失预测",
        ] + (["⑤ 交付四去向（邮件/Lark/状态页/通知）"] if args.deliver else []))
        + "\n（进度实时打印在下面；另开终端看：docker compose logs -f churn-app）")
    logger.info("启动流水线 | 测试模式=%s | 地址上限=%d | DB=%s",
                config.TEST_MODE, config.ADDRESS_LIMIT, config.DB_PATH)
    results = run_pipeline(skip_fetch=args.skip_fetch, total_stages=total_stages)

    if args.deliver:
        step_t0 = progress.stage(total_stages, total_stages, "交付四去向（邮件/Lark/状态页/通知）")
        # 四去向交付：与 scripts/daily_run.py 共用 src/deliver.py（同一口径），
        # 区别只是这里拿的是「本次运行在内存里的」预测结果，不必回头读 JSON。
        try:
            payload = (results.get("churn_model") or (None, None, {}))[2] or {}
            fs = payload.get("forecast_summary", {}) if isinstance(payload, dict) else {}
            deliver.deliver(fs, started=started, results=results, send=not args.no_send,
                            open_when_done=not args.no_open)
        except Exception as exc:  # noqa: BLE001
            deliver.deliver_failure(str(exc).strip() or exc.__class__.__name__,
                                    started=started, error_tb=traceback.format_exc(),
                                    open_when_done=not args.no_open)
        progress.stage_done(total_stages, total_stages, "交付四去向（邮件/Lark/状态页/通知）", step_t0)
    else:
        # 兼容旧行为：只做「阈值触发」的邮件/Lark 告警（不含简报卡片与状态页）。
        try:
            summary = (results.get("churn_model") or (None, None, {}))[2] or {}
            if isinstance(summary, dict) and summary.get("forecast_summary") and not args.no_alert:
                notify.send_alerts(summary["forecast_summary"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("告警环节跳过: %s", exc)

    ok = all(v[0] == "ok" for v in results.values())
    logger.info("流水线结束：%s", "全部成功" if ok else "存在失败阶段")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
