"""全流程进度显示：阶段编号 + 进度条 + 百分比 + 耗时。

为什么单独一层
-------------
``docker compose up -d`` 之后，整条流水线是在容器里跑的。用户盯着一个「容器已启动」
就没了下文，很容易以为命令没生效。这里统一提供**看得见**的进度：

* :func:`stage` / :func:`stage_done` —— 阶段级：``[3/5] ▶ 聚类分析`` → ``✅ [3/5] …（12.4s）``；
* :func:`bar` —— 长循环级（抓取地址）的行内进度条：``[████████░░░░] 62% 1245/2000``；
* :func:`banner` —— 关键节点的整行标题（一键启动时打印步骤清单）。

设计要点
--------
进度既要在交互终端里好看，也要能在 ``docker compose logs`` / ``nohup … > x.log``
里**被看见**，所以默认**打印整行**（不依赖 ``\\r``）；只有 stdout 是 TTY 时才用
``\\r`` 原地刷新。想强制关闭/开启：``CHURN_PROGRESS=0|1``。
"""
from __future__ import annotations

import os
import sys
import time

BAR_WIDTH = 24
_T0 = time.time()
_PREFIX = "churn"


def _inline() -> bool:
    """是否允许 ``\\r`` 原地刷新（TTY）。``CHURN_PROGRESS=0`` 可整体关闭。"""
    if os.getenv("CHURN_PROGRESS", "").strip() == "0":
        return False
    return sys.stdout.isatty()


def _enabled() -> bool:
    return os.getenv("CHURN_PROGRESS", "").strip() != "0"


def bar(done: int, total: int, label: str = "", width: int = BAR_WIDTH,
        prefix: str = "") -> str:
    """返回一行进度条字符串（不换行、不刷新）。

    ``done=1245, total=2000`` → ``[███████████████░░░░░░░░░] 62% 1245/2000 抓取地址``
    """
    total = max(int(total), 1)
    done = max(0, min(int(done), total))
    ratio = done / total
    filled = int(round(ratio * width))
    track = "█" * filled + "░" * (width - filled)
    text = f"{prefix}[{track}] {ratio * 100:3.0f}% {done}/{total}"
    return f"{text} {label}".rstrip()


def emit(line: str) -> None:
    """打印一行进度（TTY 下原地覆盖上一行，否则整行追加 —— 容器日志要能看见）。"""
    if not _enabled():
        return
    if _inline():
        print("\r\033[K" + line, end="", flush=True)
    else:
        print(line, flush=True)


def endline() -> None:
    """结束一次 ``\\r`` 刷新（换行），避免后续日志被进度条覆盖。"""
    if _inline():
        print(flush=True)


def banner(text: str, char: str = "=", width: int = 68) -> None:
    """整行标题（一键启动的步骤清单 / 关键节点）。"""
    if not _enabled():
        return
    print(char * width, flush=True)
    print(text, flush=True)
    print(char * width, flush=True)


def stage(index: int, total: int, title: str, note: str = "") -> float:
    """标记「第 index/total 个阶段开始」，返回起始时间戳（配合 :func:`stage_done`）。"""
    if _enabled():
        endline()
        suffix = f" {note}" if note else ""
        print(f"[{index}/{total}] ▶ {title} …{suffix}", flush=True)
    return time.time()


def stage_done(index: int, total: int, title: str, started: float,
               note: str = "") -> None:
    """标记阶段结束（含耗时与总耗时占比）。"""
    if not _enabled():
        return
    elapsed = time.time() - started
    suffix = f" {note}" if note else ""
    print(f"✅ [{index}/{total}] {title} 完成（{elapsed:.1f}s，总耗时 {time.time() - _T0:.0f}s）{suffix}",
          flush=True)
