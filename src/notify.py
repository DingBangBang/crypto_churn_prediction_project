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

import base64
import json
import logging
import re
import smtplib
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
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
def _proxy_tunnel(host: str, port: int, proxy_url: str, timeout: int = 20) -> socket.socket:
    """Open a raw TCP socket to ``host:port`` through an HTTP proxy (CONNECT).

    Needed on networks that let TCP connect but silently drop the SMTP TLS
    handshake (measured on a CN residential line: ``openssl s_client`` to
    smtp.gmail.com:465 hangs without a proxy, but completes with one).
    """
    if "//" not in proxy_url:
        proxy_url = "http://" + proxy_url
    parsed = urllib.parse.urlparse(proxy_url)
    sock = socket.create_connection((parsed.hostname, parsed.port or 8080), timeout=timeout)
    sock.sendall(
        f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()
    )
    banner = b""
    while b"\r\n\r\n" not in banner:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("代理连接被关闭（CONNECT 无响应）")
        banner += chunk
    status = banner.split(b"\r\n", 1)[0].decode(errors="replace")
    if " 200" not in status:
        raise OSError(f"代理 CONNECT 失败: {status}")
    return sock


def _smtp_ssl_via_proxy(context: ssl.SSLContext, timeout: int = 20):
    """Return a connected ``SMTP_SSL`` client whose socket rides the proxy tunnel."""
    raw = _proxy_tunnel(config.SMTP_HOST, config.SMTP_PORT, config.SMTP_PROXY, timeout)
    server = smtplib.SMTP_SSL(context=context, timeout=timeout)
    server.sock = context.wrap_socket(raw, server_hostname=config.SMTP_HOST)
    server.file = server.sock.makefile("rb")
    code, msg = server.getreply()          # SMTP greeting (220)
    if code != 220:
        raise smtplib.SMTPException(f"SMTP 问候异常: {code} {msg}")
    return server


# --- Gmail REST API (OAuth2) ---------------------------------------------------
# Google 已下线「应用专用密码」，且 Gmail API 走 https(443)，比 SMTP:465 更容易穿透出站策略。
# 一次性授权：python -m src.notify --oauth-login
def _gmail_access_token() -> str:
    """Exchange the stored refresh token for a short-lived access token."""
    resp = requests.post(config.GOOGLE_TOKEN_URL, data={
        "client_id": config.GMAIL_CLIENT_ID,
        "client_secret": config.GMAIL_CLIENT_SECRET,
        "refresh_token": config.GMAIL_REFRESH_TOKEN,
        "grant_type": "refresh_token"}, timeout=20, proxies=_proxies())
    if resp.status_code != 200:
        raise OSError(f"刷新 access_token 失败 {resp.status_code}: {resp.text[:200]}")
    return resp.json()["access_token"]


def send_email_via_gmail_api(msg: MIMEMultipart) -> bool:
    """Send a MIME message through ``gmail.googleapis.com`` (needs the GMAIL_* vars)."""
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    resp, via = _post_json(config.GMAIL_SEND_URL, {"raw": raw}, timeout=25,
                           headers={"Authorization": f"Bearer {_gmail_access_token()}"})
    ok = resp.status_code == 200
    logger.info("Gmail API 邮件%s（经%s）",
                f"已发送 -> {msg['To']}" if ok else f"失败 {resp.status_code}: {resp.text[:160]}", via)
    return ok


def oauth_login(timeout_s: int = 300) -> int:
    """One-off helper: walk the Google consent screen and print GMAIL_REFRESH_TOKEN."""
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    if not (config.GMAIL_CLIENT_ID and config.GMAIL_CLIENT_SECRET):
        logger.error("请先在 environment.env 里填 GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET")
        return 1
    redirect = config.GMAIL_REDIRECT_URI
    parsed = urllib.parse.urlsplit(redirect)
    captured: Dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            captured.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h3>授权完成，请回到终端。</h3>".encode("utf-8"))

        def log_message(self, *args):  # keep the console clean
            return

    url = config.GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": config.GMAIL_CLIENT_ID, "redirect_uri": redirect,
        "response_type": "code", "scope": config.GMAIL_SCOPE,
        "access_type": "offline", "prompt": "consent"})
    server = HTTPServer((parsed.hostname or "localhost", parsed.port or 80), Handler)
    server.timeout = timeout_s
    logger.info("已打开浏览器完成一次性授权；若未自动打开请手动访问:\n%s", url)
    try:
        webbrowser.open(url)
    except Exception:  # pragma: no cover
        pass
    server.handle_request()                 # blocks until Google bounces back
    server.server_close()
    if "code" not in captured:
        logger.error("未拿到授权码（超时或被拒绝）: %s", captured.get("error", "timeout"))
        return 1

    resp = requests.post(config.GOOGLE_TOKEN_URL, data={
        "client_id": config.GMAIL_CLIENT_ID, "client_secret": config.GMAIL_CLIENT_SECRET,
        "code": captured["code"], "grant_type": "authorization_code",
        "redirect_uri": redirect}, timeout=20, proxies=_proxies())
    if resp.status_code != 200:
        logger.error("换取 token 失败 %s: %s", resp.status_code, resp.text[:200])
        return 1
    token = resp.json()
    print("\n把下面这行写入 environment.env（该文件已被 .gitignore 忽略）:\n")
    print(f"GMAIL_REFRESH_TOKEN={token.get('refresh_token', '')}\n")
    if not token.get("refresh_token"):
        print("⚠️ 未返回 refresh_token：请到 Google 账号「第三方访问」中撤销本应用后重试一次。\n")
    return 0


def send_email(subject: str, html_body: str, to: str | None = None) -> bool:
    to = to or config.ALERT_EMAIL_TO
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.SMTP_USER or config.ALERT_EMAIL_TO
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # Prefer the OAuth2 Gmail API when it is configured (Google 已下线应用专用密码）。
    if config.GMAIL_CLIENT_ID and config.GMAIL_CLIENT_SECRET and config.GMAIL_REFRESH_TOKEN:
        try:
            return send_email_via_gmail_api(msg)
        except Exception as exc:
            logger.error("Gmail API 发信失败: %s", exc)
            return False

    if not (config.SMTP_USER and config.SMTP_PASS):
        logger.warning("邮件未发送：缺少凭证（需 GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN "
                       "或 SMTP_USER/SMTP_PASS）")
        return False
    try:
        ctx = ssl.create_default_context()
        if config.SMTP_PROXY:
            server = _smtp_ssl_via_proxy(ctx)
            logger.info("邮件经代理隧道连接: %s", config.SMTP_PROXY)
        else:
            server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,
                                      context=ctx, timeout=20)
        try:
            server.ehlo()
            # Gmail 同时支持 LOGIN/PLAIN/XOAUTH2。若密码错误，smtplib 会换下一种机制重试，
            # 而 Gmail 在首次拒绝后直接断连，真实的 "535 BadCredentials" 就被换成了一句
            # 费解的 "Connection unexpectedly closed"。锁死为 PLAIN，让报错保持可读。
            if server.esmtp_features.get("auth"):
                server.esmtp_features["auth"] = "PLAIN"
            server.login(config.SMTP_USER, config.SMTP_PASS)
            server.sendmail(config.SMTP_USER, [to], msg.as_string())
        finally:
            server.close()
        logger.info("预警邮件已发送 -> %s", to)
        return True
    except smtplib.SMTPAuthenticationError as exc:
        detail = exc.smtp_error
        detail = detail.decode(errors="replace") if isinstance(detail, bytes) else str(detail)
        logger.error("邮件发送失败：SMTP 认证被拒 -> %s\n"
                     "  · Gmail 必须使用 16 位【应用专用密码】(https://myaccount.google.com/apppasswords)，"
                     "不是账号登录密码；\n"
                     "  · 若确认密码正确，请检查 SMTP_PROXY 是否可用。", detail.strip())
        return False
    except Exception as exc:  # pragma: no cover - depends on external SMTP
        logger.error("邮件发送失败: %s", exc)
        return False


# --- egress proxy + HTTP helpers ----------------------------------------------
def _proxies() -> Dict[str, str] | None:
    """``requests`` proxy map, or None when no egress proxy is configured."""
    proxy = config.NET_PROXY
    return {"http": proxy, "https": proxy} if proxy else None


def _post_json(url: str, payload: dict, timeout: int = 15, prefer_proxy: bool = True,
               headers: Dict[str, str] | None = None):
    """POST JSON, falling back between the egress proxy and a direct connection.

    Google/Lark endpoints are unreliable on mainland networks (TCP connects, but the
    TLS handshake gets black-holed), so prefer the proxy when one is configured and
    fall back to a direct call.  Returns ``(response, "代理"|"直连")``.
    """
    proxy_map = _proxies()
    if proxy_map and prefer_proxy:
        order = [(proxy_map, "代理"), (None, "直连")]
    elif proxy_map:
        order = [(None, "直连"), (proxy_map, "代理")]
    else:
        order = [(None, "直连")]

    last: Exception | None = None
    for proxies, label in order:
        try:
            return requests.post(url, json=payload, timeout=timeout,
                                 proxies=proxies, headers=headers), label
        except Exception as exc:  # pragma: no cover - network dependent
            last = exc
            logger.warning("%s 经%s请求失败: %s", urllib.parse.urlsplit(url).netloc, label, exc)
    raise last if last else RuntimeError("请求失败")


# --- Lark (Feishu / Lark) incoming webhook -------------------------------------
def _lark_payload(text: str) -> Dict[str, object]:
    """Text payload; @-mention only when a real Lark id (ou_/on_/all) is configured.

    旧实现把手机号当 open_id 塞进 ``<at user_id="133...">``，Lark 无法解析会导致整条
    消息被拒。手机号必须先换成 open_id 才能 @，这里做了护栏。
    """
    target = (config.LARK_AT_ID or "").strip()
    if target.startswith(("ou_", "on_", "all")):
        text = f'<at user_id="{target}"></at>\n{text}'
    return {"msg_type": "text", "content": {"text": text}}


def send_lark(text: str) -> bool:
    if not config.LARK_WEBHOOK_URL:
        logger.warning("Lark 未发送：缺少 LARK_WEBHOOK_URL")
        return False
    try:
        resp, via = _post_json(config.LARK_WEBHOOK_URL, _lark_payload(text), timeout=15)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        ok = resp.status_code == 200 and body.get("code", 0) == 0
        logger.info("Lark 推送%s（经%s）", "已发送" if ok else
                    f"返回异常 {resp.status_code}: {resp.text[:160]}", via)
        return bool(ok)
    except Exception as exc:  # pragma: no cover - network dependent
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


# --- clustering digest (text form) ---------------------------------------------
# The Lark bot must push business-readable clustering results *and* the insights
# distilled from them, not only the churn warning.  Everything is aggregated straight
# from SQLite so the pushed text always matches what the dashboard shows.
NOISE_LABEL = "噪音/机器人"                    # keep in sync with src/cluster_analyzer.NOISE_LABEL
RISK_LABELS = {"高频套利者", "噪音/机器人"}     # keep in sync with scripts/daily_run.RISK_LABELS
PERSONA_NOTES = {
    "高频大户": "交易频繁、余额厚，是协议的核心用户",
    "长期持有者": "低频但持仓久，粘性最强、流失率最低",
    "DeFi农民": "跨协议搬砖、协议多样性极高，对收益率极敏感",
    "高频套利者": "高频 Gas 占比高、长期闲置，最易流失",
    NOISE_LABEL: "一次性地址或脚本机器人（HDBSCAN 噪音点）",
}
CLUSTER_PROFILE_SQL = """
SELECT ac.cluster_label          AS label,
       COUNT(*)                  AS n,
       AVG(f.tx_freq_daily)      AS freq,
       AVG(f.avg_holding_hours)  AS holding,
       AVG(f.protocol_diversity) AS protocols,
       AVG(f.high_gas_ratio)     AS gas_ratio,
       AVG(f.eth_balance_end)    AS balance,
       AVG(f.last_tx_days_ago)   AS idle_days,
       AVG(cp.is_churned)        AS churn_rate
FROM address_clusters ac
JOIN address_features f ON f.address = ac.address
LEFT JOIN churn_predictions cp ON cp.address = ac.address
GROUP BY ac.cluster_label
ORDER BY n DESC
"""


def collect_cluster_digest(db_path: str | Path | None = None) -> Dict[str, object]:
    """Aggregate the persona distribution + behaviour profile of every cluster."""
    import sqlite3

    path = Path(db_path or config.DB_PATH)
    digest: Dict[str, object] = {"rows": [], "n_addresses": 0, "n_personas": 0,
                                 "n_noise": 0, "risk_ratio": 0.0}
    if not path.exists():
        logger.warning("聚类摘要跳过：数据库不存在 (%s)", path)
        return digest
    try:
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute(CLUSTER_PROFILE_SQL).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("聚类摘要跳过：%s", exc)
        return digest

    total = sum(int(r[1] or 0) for r in rows)
    digest["rows"] = [{
        "label": r[0], "n": int(r[1] or 0),
        "pct": (int(r[1] or 0) / total) if total else 0.0,
        "freq": r[2], "holding": r[3], "protocols": r[4], "gas_ratio": r[5],
        "balance": r[6], "idle_days": r[7], "churn_rate": r[8],
    } for r in rows]
    digest["n_addresses"] = total
    digest["n_personas"] = sum(1 for r in digest["rows"] if r["label"] != NOISE_LABEL)
    digest["n_noise"] = sum(r["n"] for r in digest["rows"] if r["label"] == NOISE_LABEL)
    risky = sum(r["n"] for r in digest["rows"] if r["label"] in RISK_LABELS)
    digest["risk_ratio"] = (risky / total) if total else 0.0
    return digest


def _num(value, digits: int = 1) -> str:
    """Format a possibly-None/NaN SQL aggregate for chat output."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "-"
    if num != num:                              # NaN
        return "-"
    return f"{num:.{digits}f}"


def _pct(value, digits: int = 1) -> str:
    """Format a 0-1 ratio as a percentage for chat output."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "-"
    if num != num:                              # NaN
        return "-"
    return f"{num:.{digits}%}"


def render_cluster_text(digest: Dict[str, object]) -> str:
    """Turn the digest into a chat-friendly, business-worded block."""
    rows = digest.get("rows") or []
    if not rows:
        return "【用户行为聚类结果】暂无可用的聚类数据（请先跑完聚类阶段）。"
    lines = [
        f"【用户行为聚类结果】HDBSCAN · {digest['n_personas']} 类 + {digest['n_noise']} 个噪音点"
        f" · 覆盖 {digest['n_addresses']} 个地址"
    ]
    for r in rows:
        mark = "⚠️ " if r["label"] in RISK_LABELS else ""
        lines.append(
            f"{mark}{r['label']} · {r['n']} 个（{r['pct']:.1%}）｜实际流失率 {_pct(r['churn_rate'])}"
            f" · 日均 {_num(r['freq'], 2)} 笔 · 持仓 {_num(r['holding'], 1)}h"
            f" · 协议 {_num(r['protocols'], 1)} 个 · 闲置 {_num(r['idle_days'], 1)} 天"
        )
        note = PERSONA_NOTES.get(r["label"])
        if note:
            lines.append(f"    └ {note}")
    lines.append(f"高危行为簇占比 {digest['risk_ratio']:.1%}（{'、'.join(sorted(RISK_LABELS))}）")
    return "\n".join(lines)


_INSIGHT_KEEP = ("驱动流失的关键特征", "未来 30 天 ARIMA 预测", "结论")


def _markdown_to_text(md: str) -> str:
    """Flatten the auto-generated insights markdown into chat-friendly plain text."""
    out: List[str] = []
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("#"):
            continue                        # headings are re-added by the caller
        if line.startswith("---") or line.startswith(">"):
            continue                        # 文件分隔线与「自动生成」说明
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        line = re.sub(r"`([^`]+)`", r"\1", line)
        if line.startswith("- "):
            line = "• " + line[2:]
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def read_insights(max_chars: int = 1200) -> str:
    """Distil ``docs/insights.md`` down to the few sections worth pushing to a group."""
    path = config.DOCS_DIR / "insights.md"
    if not path.exists():
        return ""
    md = path.read_text(encoding="utf-8")
    blocks: Dict[str, List[str]] = {}
    current: str | None = None
    for raw in md.splitlines():
        if raw.startswith("### "):
            title = raw[4:].strip()
            if title in blocks:             # 旧版本曾把整篇正文追加两遍，只保留第一份
                current = None
            else:
                blocks[title] = []
                current = title
            continue
        if current:
            blocks[current].append(raw)

    picked: List[str] = []
    for title in _INSIGHT_KEEP:
        matched = next((k for k in blocks if k.startswith(title)), None)
        if matched:
            body = _markdown_to_text("\n".join(blocks[matched]))
            if body:
                picked.append(f"· {matched}\n{body}")
    text = "\n".join(picked) or _markdown_to_text(md)[:max_chars]
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + " …"
    return text


def build_digest_text(summary: Dict[str, float] | None = None,
                      risk_cluster_ratio: float = 0.0,
                      digest: Dict[str, object] | None = None,
                      insights: str | None = None) -> str:
    """Compose the daily Lark push: churn warning + clustering results + insights."""
    summary = summary or {}
    digest = digest if digest is not None else collect_cluster_digest()
    triggered, reasons, context = evaluate_alerts(summary, risk_cluster_ratio)
    horizon = summary.get("horizon", config.FORECAST_HORIZON_DAYS)
    rule = "————————————"
    return "\n".join([
        "📮 加密用户行为聚类 & 流失预测 · 每日简报",
        f"{context['generated_at']} ｜ 预测时间窗 {horizon} 天",
        rule,
        "一、流失预警（" + ("已触发 ⚠️" if triggered else "未达阈值，仅例行播报") + "）",
        f"预测流失率（{horizon} 天）: {context['churn_rate']}（阈值 {context['churn_rate_threshold']}）",
        f"高危流失占比: {context['high_risk_ratio']}（阈值 {context['risk_ratio_threshold']}）"
        f"　高危行为簇占比: {risk_cluster_ratio:.1%}",
        f"人均日交易频次: {summary.get('current_mean_freq', '-')} → 第 {horizon} 天 "
        f"{summary.get('final_day_freq', '-')}（降幅 {context['freq_drop']}）",
        "触发原因: " + ("；".join(reasons) if reasons else "未达阈值，仅例行播报"),
        rule,
        "二、" + render_cluster_text(digest),
        rule,
        "三、业务洞察（从聚类与模型自动提炼）",
        (insights if insights is not None else read_insights())
        or "（暂无 docs/insights.md，请先运行 churn_model）",
        rule,
        f"看板: {config.DASHBOARD_URL}",
    ])


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
                force: bool = False, digest: Dict[str, object] | None = None) -> bool:
    """Render + dispatch e-mail & Lark alert (and a macOS banner). Returns True if sent."""
    triggered, reasons, context = evaluate_alerts(summary, risk_cluster_ratio)
    if not triggered and not force:
        logger.info("未达到预警阈值，跳过告警发送")
        return False

    subject = f"⚠️ 加密用户流失预警 · 流失率 {context['churn_rate']} · 高危占比 {context['high_risk_ratio']}"
    html_body = render_email(context)
    email_ok = send_email(subject, html_body)

    # Lark gets the full picture: warning + clustering results + distilled insights.
    lark_ok = send_lark(build_digest_text(summary, risk_cluster_ratio, digest=digest))
    notify_macos(f"流失率 {context['churn_rate']}，高危 {context['high_risk_ratio']}",
                 title="⚠️ 加密用户流失预警")
    logger.info("告警发送: email=%s, lark=%s", email_ok, lark_ok)
    return email_ok or lark_ok


def send_lark_digest(summary: Dict[str, float] | None = None,
                     risk_cluster_ratio: float | None = None,
                     digest: Dict[str, object] | None = None) -> bool:
    """Always-on daily push: clustering results (text form) + distilled insights.

    Unlike :func:`send_alerts` this is *not* gated by the churn thresholds — the group
    should receive the clustering picture every day; a crossed threshold merely turns
    the warning line red (⚠️).
    """
    digest = digest if digest is not None else collect_cluster_digest()
    if risk_cluster_ratio is None:
        risk_cluster_ratio = float(digest.get("risk_ratio") or 0.0)
    return send_lark(build_digest_text(summary, risk_cluster_ratio, digest=digest))


def _report_summary() -> Dict[str, float]:
    """Prefer the real numbers of the last pipeline run; fall back to demo values."""
    path = config.REPORTS_DIR / config.artefact("churn_summary.json")
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            summary = data.get("forecast_summary") or {}
            if summary:
                logger.info("使用真实预测结果: %s", path.name)
                return summary
        except Exception as exc:
            logger.warning("读取 %s 失败: %s", path.name, exc)
    logger.info("未找到真实预测结果，改用示例数字演示")
    return {"forecast_churn_rate": 0.47, "high_risk_ratio": 0.31, "avg_freq_drop": 1.8,
            "current_mean_freq": 2.4, "final_day_freq": 0.6, "horizon": 30}


def main(argv: List[str] | None = None) -> int:
    """``python -m src.notify``              → live test push (e-mail + Lark), real numbers

    ``python -m src.notify --oauth-login`` → one-off Google consent, prints the refresh token
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--oauth-login" in argv:
        return oauth_login()

    summary = _report_summary()
    digest = collect_cluster_digest()
    risk_ratio = float(digest.get("risk_ratio") or 0.0)
    _, _, context = evaluate_alerts(summary, risk_ratio)
    subject = (f"[测试] ⚠️ 加密用户流失预警 · 流失率 {context['churn_rate']}"
               f" · 高危占比 {context['high_risk_ratio']}")
    email_ok = send_email(subject, render_email(context))
    lark_ok = send_lark(build_digest_text(summary, risk_ratio, digest=digest))
    logger.info("测试推送结果: email=%s, lark=%s", email_ok, lark_ok)
    return 0 if (email_ok or lark_ok) else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
