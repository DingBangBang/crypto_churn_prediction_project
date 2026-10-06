"""End-to-end pipeline runner: fetch -> features -> clusters -> churn model.

This is the single entry-point used by ``docker-entrypoint.sh`` and by anyone who
wants to reproduce the whole result with one command. Each stage is imported from
``src`` and run in order; a failing stage is logged but does not abort later stages
so the dashboard always has as much data as possible.

Usage
-----
python scripts/run_pipeline.py                # full run (respects ADDRESS_LIMIT)
python scripts/run_pipeline.py --skip-fetch   # reuse existing raw data
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import churn_model, cluster_analyzer, config, data_fetcher, db  # noqa: E402
from src import feature_engineer, notify  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("churn.pipeline")


def _stage(name, fn, results):
    t0 = time.time()
    try:
        out = fn()
        results[name] = ("ok", round(time.time() - t0, 1), out)
        logger.info("✅ %s 完成 (%.1fs)", name, time.time() - t0)
    except Exception as exc:  # noqa: BLE001
        results[name] = ("fail", round(time.time() - t0, 1), str(exc))
        logger.error("❌ %s 失败: %s", name, exc)
    return results


def run_pipeline(skip_fetch: bool = False) -> dict:
    results: dict = {}
    if not skip_fetch:
        _stage("data_fetcher", data_fetcher.fetch_population, results)
    else:
        logger.info("跳过 data_fetcher（--skip-fetch）")

    _stage("feature_engineer",
           lambda: feature_engineer.write_features(feature_engineer.compute_features(
               feature_engineer.load_transactions())),
           results)
    _stage("cluster_analyzer", cluster_analyzer.run, results)
    _stage("churn_model", churn_model.run, results)
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the full crypto churn pipeline.")
    parser.add_argument("--skip-fetch", action="store_true", help="reuse existing raw data")
    parser.add_argument("--no-alert", action="store_true", help="do not send alerts")
    args = parser.parse_args(argv)

    logger.info("启动流水线 | 测试模式=%s | 地址上限=%d | DB=%s",
                config.TEST_MODE, config.ADDRESS_LIMIT, config.DB_PATH)
    results = run_pipeline(skip_fetch=args.skip_fetch)

    # Alerts are evaluated from the churn-model output written to the DB.
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
