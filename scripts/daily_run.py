"""Daily scheduled job (15:30 via Cline Schedule / cron) —— 四去向的「定时」入口。

职责边界（重要）
----------------
* 本脚本 = **跑流水线**（``scripts/run_pipeline.py``）+ **交付四个去向**；
* 交付本身住在 ``src/deliver.py``；容器首启走的是
  ``scripts/run_pipeline.py --deliver`` —— 同一个 ``deliver()``，所以邮件/Lark/状态页
  的口径在「本机定时」与「docker compose 一键」两条路径上完全一致。

Usage
-----
python scripts/daily_run.py                 # 完整：抓取 + 特征/聚类/预测 + 四去向
python scripts/daily_run.py --dry-run       # 跳过抓取（复用已有原始数据），其余照旧
python scripts/daily_run.py --deliver-only  # 只做交付（读 churn_summary.json，不跑任何阶段）
python scripts/daily_run.py --no-send       # 不真的发邮件/Lark（自检用）
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

from src import config, deliver  # noqa: E402
from scripts.run_pipeline import run_pipeline  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("churn.daily")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="每日任务：流水线 + 邮件/Lark/状态页/通知")
    parser.add_argument("--dry-run", action="store_true", help="skip the network fetch")
    parser.add_argument("--deliver-only", action="store_true",
                        help="跳过流水线，直接用上次的预测结果交付四个去向")
    parser.add_argument("--no-send", action="store_true", help="不发送邮件/Lark（自检）")
    parser.add_argument("--no-open", action="store_true", help="不自动打开状态页")
    args = parser.parse_args(argv)

    started = time.time()
    open_when_done = not args.no_open
    try:
        if args.deliver_only:
            logger.info("--deliver-only：跳过流水线，复用 %s",
                        config.REPORTS_DIR / config.artefact("churn_summary.json"))
            out = deliver.deliver(started=started, send=not args.no_send,
                                  open_when_done=open_when_done)
        else:
            results = run_pipeline(skip_fetch=args.dry_run)
            payload = (results.get("churn_model") or (None, None, {}))[2] or {}
            fs = payload.get("forecast_summary", {}) if isinstance(payload, dict) else {}
            out = deliver.deliver(fs, started=started, results=results,
                                  send=not args.no_send, open_when_done=open_when_done)
        logger.info("每日任务成功 -> %s", out["status_page"])
        # 看板进程不会热更新口径（config 常量在启动时冻结）：数据刚变时主动提醒一次。
        logger.info(
            "提示：若看板页面仍显示旧数据，说明看板进程早于本次运行 —— "
            "重启看板：容器 `docker compose restart churn-app` / "
            "宿主机 `pkill -f 'streamlit run app/dashboard.py'` 后重新运行"
        )
        return 0 if out["ok"] else 1
    except Exception as exc:  # noqa: BLE001
        reason = str(exc).strip() or exc.__class__.__name__
        deliver.deliver_failure(reason, started=started, error_tb=traceback.format_exc(),
                                open_when_done=open_when_done)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
