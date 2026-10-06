"""Alerting layer: e-mail (HTML template) + Lark (Feishu) webhook + macOS banner.

Design
------
* The e-mail body lives in ``templates/alert_email.html`` and keeps ``{{placeholders}}``
  so every alert can be filled with that run's numbers.
* Lark (Feishu) receives a rich-text (markdown-ish) card via an incoming webhook and
  @-mentions the configured phone number.
* macOS uses ``osascript`` banners, matching the Whale project's UX.
* Every send is best-effort: a missing credential logs a warning instead of raising,
  so a poorly-configured alert channel never breaks the nightly pipeline.

Usage
-----
python -m src.notify          # sends a demo alert with sample numbers
"""
from __future__ import annotations

import json
import logging
import smtplib
import ssl
import subprocess
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Tuple

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402

logger = logging.getLogger("churn.notify")
TEMPLATE_PATH = ROOT / "templates" / "alert_email.html"


# --- rendering ----------------------------------------------------------------
def render_email(context: Dict[str, str]) -> str:
    """Fill the HTML template with ``context`` (missing keys collapse to '-')."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    for key, value in context.items():
        template = template.replace("{{" + key + "}}", str(value))
    # Any placeholder we forgot to provide.
    import re
    template = re.sub(r"\{\{[a-zA-Z0-9_]+\}\}", "-", template)
    return template


def _detail_rows(items: List[Tuple[str, str]]) -> str:
    rows = []
    for k, v in items:
        rows.append(
            f'<tr><td style="padding:8px 12px;border:1px solid #e5e7eb;">{k}</td>'
            f'<td style="padding:8px 12px;border:1px solid #e5e7eb;text-align:right;">{v}</td></tr>'
        )
    return "\n".join(rows)


# --- channels -----------------------------------------------------------------
def send_email(subject: str, html_body: str, to: str | None = None) -> bool:
    to = to or config.ALERT_EMAIL_TO
    if not (config.SMTP_USER and config.SMTP_PASS):
        logger.warning("邮件未发送：缺少 SMTP_USER / SMTP_PASS")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.SMTP_USER
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ctx, timeout=20) as server:
            server.login(config.SMTP_USER, config.SMTP_PASS)
            server.sendmail(config.SMTP_USER, [to], msg.as_string())
        logger.info("预警邮件已发送 -> %s", to)
        return True
    except Exception as exc:  # pragma: no cover - depends on external SMTP
        logger.error("邮件发送失败: %s", exc)
        return False


def send_lark(text: str) -> bool:
    if not config.LARK_WEBHOOK_URL:
        logger.warning("Lark 未发送：缺少 LARK_WEBHOOK_URL")
        return False
    payload = {
        "msg_type": "text",
        "content": {"text": f"<at user_id=\"{config.LARK_AT_PHONE}\"></at>\n{text}"},
    }
    try:
        resp = requests.post(config.LARK_WEBHOOK_URL, json=payload, timeout=15)
        ok = resp.status_code == 200 and resp.json().get("code", 0) == 0
        logger.info("Lark 告警%s", "已发送" if ok else f"返回异常: {resp.text[:120]}")
        return ok
    except Exception as exc:  # pragma: no cover
        logger.error("Lark 发送失败: %s", exc)
        return False


def notify_macos(message: str, title: str = "加密用户流失预警") -> None:
    if sys.platform != "darwin":
        return
    try:
        esc = '"' + str(message).replace("\\", "\\\\").replace('"', '\\"') + '"'
        ttl = '"' + str(title).replace('"', '\\"') + '"'
        subprocess.run(["osascript", "-e", f"display notification {esc} with title {ttl}"],
                       check=False, timeout=10)
    except Exception:  # pragma: no cover
        pass


# --- decision + dispatch ------------------------------------------------------
def evaluate_alerts(summary: Dict[str, float], risk_cluster_ratio: float = 0.0):
    """Return (triggered, reasons, context) based on the configured thresholds."""
    churn_rate = float(summary.get("forecast_churn_rate", 0) or 0)
    high_risk = float(summary.get("high_risk_ratio", 0) or 0)
    freq_drop = float(summary.get("avg_freq_drop", 0) or 0)

    reasons: List[str] = []
    if churn_rate >= config.ALERT_CHURN_RATE_THRESHOLD:
        reasons.append(
            f"预测 30 天流失率 {churn_rate:.1%} ≥ 阈值 {config.ALERT_CHURN_RATE_THRESHOLD:.0%}")
    if high_risk >= config.ALERT_RISK_CLUSTER_RATIO:
        reasons.append(
            f"高危地址占比 {high_risk:.1%} ≥ 阈值 {config.ALERT_RISK_CLUSTER_RATIO:.0%}")
    if risk_cluster_ratio >= config.ALERT_RISK_CLUSTER_RATIO * 2:
        reasons.append(f"高危行为簇占比 {risk_cluster_ratio:.1%} 异常偏高")

    triggered = bool(reasons)
    level = "高 (High)" if (churn_rate >= config.ALERT_CHURN_RATE_THRESHOLD * 1.5) else "中 (Medium)"
    context = {
        "alert_level": level,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "churn_rate": f"{churn_rate:.1%}",
        "churn_rate_threshold": f"{config.ALERT_CHURN_RATE_THRESHOLD:.0%}",
        "high_risk_ratio": f"{high_risk:.1%}",
        "risk_ratio_threshold": f"{config.ALERT_RISK_CLUSTER_RATIO:.0%}",
        "freq_drop": f"{freq_drop:.3f}",
        "trigger_reasons": "<br>".join(f"• {r}" for r in reasons) or "• 未触发（仅例行通知）",
        "dashboard_url": config.DASHBOARD_URL,
        "detail_rows": _detail_rows([
            ("预测流失率 (30天)", f"{churn_rate:.2%}"),
            ("高危地址占比", f"{high_risk:.2%}"),
            ("当前人均日交易频次", f"{summary.get('current_mean_freq', '-')}"),
            ("第30天人均日交易频次", f"{summary.get('final_day_freq', '-')}"),
            ("人均频次降低值", f"{freq_drop:.3f}"),
            ("预测窗口", f"{summary.get('horizon', config.FORECAST_HORIZON_DAYS)} 天"),
        ]),
    }
    return triggered, reasons, context


def send_alerts(summary: Dict[str, float], risk_cluster_ratio: float = 0.0,
                force: bool = False) -> bool:
    """Render + dispatch e-mail & Lark alert (and a macOS banner). Returns True if sent."""
    triggered, reasons, context = evaluate_alerts(summary, risk_cluster_ratio)
    if not triggered and not force:
        logger.info("未达到预警阈值，跳过告警发送")
        return False

    subject = f"⚠️ 加密用户流失预警 · 流失率 {context['churn_rate']} · 高危占比 {context['high_risk_ratio']}"
    html_body = render_email(context)
    email_ok = send_email(subject, html_body)

    lark_text = (
        f"加密用户流失预警\n"
        f"预测流失率: {context['churn_rate']} | 高危占比: {context['high_risk_ratio']}\n"
        f"人均频次降低: {context['freq_drop']}\n"
        f"原因: {'; '.join(reasons) if reasons else '手动触发'}\n"
        f"看板: {context['dashboard_url']}"
    )
    lark_ok = send_lark(lark_text)
    notify_macos(f"流失率 {context['churn_rate']}，高危 {context['high_risk_ratio']}",
                 title="⚠️ 加密用户流失预警")
    logger.info("告警发送: email=%s, lark=%s", email_ok, lark_ok)
    return email_ok or lark_ok


def main() -> int:  # demo run
    demo = {"forecast_churn_rate": 0.47, "high_risk_ratio": 0.31, "avg_freq_drop": 1.8,
            "current_mean_freq": 2.4, "final_day_freq": 0.6, "horizon": 30}
    send_alerts(demo, force=True)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
