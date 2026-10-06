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
import html
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


def _post_lark(payload: Dict[str, object], kind: str = "文本") -> bool:
    """POST one payload to the Lark incoming webhook (shared by text & card sends)."""
    if not config.LARK_WEBHOOK_URL:
        logger.warning("Lark 未发送：缺少 LARK_WEBHOOK_URL")
        return False
    try:
        resp, via = _post_json(config.LARK_WEBHOOK_URL, payload, timeout=15)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        ok = resp.status_code == 200 and body.get("code", 0) == 0
        logger.info("Lark %s推送%s（经%s）", kind, "已发送" if ok else
                    f"返回异常 {resp.status_code}: {resp.text[:160]}", via)
        return bool(ok)
    except Exception as exc:  # pragma: no cover - network dependent
        logger.error("Lark 发送失败: %s", exc)
        return False


def send_lark(text: str) -> bool:
    """Plain-text push（``msg_type=text``）—— 卡片失败时的兜底通道。"""
    return _post_lark(_lark_payload(text), kind="文本")


def send_lark_card(card: Dict[str, object]) -> bool:
    """Markdown 消息卡片推送（``msg_type=interactive`` + ``lark_md``，支持加粗/彩色标题）。"""
    return _post_lark({"msg_type": "interactive", "card": card}, kind="卡片")


def notify_macos(message: str, title: str = "加密用户流失预警",
                 url: str | None = None) -> None:
    """macOS 系统通知。

    优先用 ``terminal-notifier``：它支持 ``-open <url>`` —— **点按通知本身就会打开**
    目标页面（本项目里是 `reports/status.html` 运行状态页，含三方向链接 + 结果内容）。
    没有装就退回 ``osascript display notification``（不支持点击跳转，仅横幅）。
    """
    if sys.platform != "darwin":
        return
    if url:
        target = str(url)
        if not target.startswith(("http://", "https://", "file://")):
            target = "file://" + str(Path(target).expanduser().resolve())
        tn = _terminal_notifier()
        if tn:
            try:
                proc = subprocess.run([tn, "-title", str(title), "-message", str(message),
                                       "-sound", "default", "-open", target],
                                      check=False, timeout=15, capture_output=True, text=True)
                noisy = (proc.stdout or "") + (proc.stderr or "")
                if proc.returncode == 0 and "not allowed" not in noisy.lower() \
                        and "turned off" not in noisy.lower():
                    return
                hint = ""
                if "turned off" in noisy.lower():
                    hint = ("｜一次性开启：系统设置 → 通知 → 找到「Terminal Notifier」允许通知"
                            "（或用 `open \"x-apple.systempreferences:"
                            "com.apple.Notifications-Settings.extension\"` 直接打开该面板）")
                logger.warning("terminal-notifier 未生效（%s），回退 osascript 横幅%s",
                               noisy.strip().splitlines()[0][:140] if noisy.strip() else "未知",
                               hint)
            except Exception:  # pragma: no cover
                pass
    try:
        esc = '"' + str(message).replace("\\", "\\\\").replace('"', '\\"') + '"'
        ttl = '"' + str(title).replace('"', '\\"') + '"'
        subprocess.run(["osascript", "-e", f"display notification {esc} with title {ttl}"],
                       check=False, timeout=10)
    except Exception:  # pragma: no cover
        pass


def _terminal_notifier() -> str | None:
    """Path of ``terminal-notifier`` if installed (enables click-to-open notifications).

    The binary **inside the ``.app`` bundle** is preferred: notifications are then attributed
    to a real app bundle, so macOS can show the permission prompt and the user can enable it in
    系统设置 → 通知 → Terminal Notifier（用公式里的裸 binary 会一直报
    ``Notifications are not allowed for this application``）。
    """
    for candidate in (
            Path.home() / "Applications/Terminal Notifier.app/Contents/MacOS/terminal-notifier",
            Path("/Applications/Terminal Notifier.app/Contents/MacOS/terminal-notifier"),
            Path("/opt/homebrew/opt/terminal-notifier/terminal-notifier.app/Contents/MacOS/terminal-notifier"),
            Path("/usr/local/opt/terminal-notifier/terminal-notifier.app/Contents/MacOS/terminal-notifier"),
            Path("/opt/homebrew/bin/terminal-notifier"),
            Path("/usr/local/bin/terminal-notifier"),
            Path("/Applications/terminal-notifier.app/Contents/MacOS/terminal-notifier")):
        if Path(candidate).exists():
            return str(candidate)
    return None


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


# --- 运行状态 + 风险等级 + 多时间窗 + 初步推进建议 --------------------------------
# 「运行状态 / 报错内容 / 多时间窗预测 / 是否高危 / 初步推进建议」全部由**纯规则**
# （模板 + 参数填充）生成，不调用任何外部大模型：稳定、可审计、零 token 成本。
# 用 LLM 生成「初步推进建议」的探索步骤见 README「下一步优化」。
RISK_LEVELS = (
    ("critical", "🔴 高危", "red", "必须立刻重视", "拉齐 增长 / 产品 / 风控 开会处置"),
    ("alert", "🟠 警戒", "orange", "已越过预警线", "进入处置流程：高危簇触达 + 召回实验"),
    ("watch", "🟡 关注", "yellow", "接近预警线", "提前准备召回策略，盯住短窗（1/7 天）走势"),
    ("normal", "🟢 正常", "green", "未触及预警线", "保持例行监控，按周复盘聚类漂移"),
)
STAGE_NAMES = {
    "data_fetcher": "① 数据抓取",
    "feature_engineer": "② 特征工程",
    "cluster_analyzer": "③ 聚类",
    "churn_model": "④ 流失预测",
}
# 每个簇 → (动作, 责任方, 依据)
CLUSTER_ACTIONS = {
    "高频套利者": ("定向召回 + 费率/返佣实验", "增长 / 运营",
                   "高频 Gas 占比高、长期闲置，对收益率与费率极敏感"),
    "噪音/机器人": ("先做女巫/刷量识别再谈留存", "风控 / 数据",
                    "一次性地址或脚本刷量，会污染留存率口径"),
    "DeFi农民": ("收益聚合与跨协议激励", "产品 / 增长",
                  "协议多样性最高，跟随收益率迁移"),
    "高频大户": ("一对一维护 + 大客户权益", "客户成功 / BD",
                  "余额厚、交易频繁，是协议收入核心"),
    "长期持有者": ("长期激励 / 治理参与提粘性", "产品",
                    "持仓久、流失率最低，适合做忠诚度运营"),
}


def _md_bold_to_text(md: str) -> str:
    """Strip Lark-markdown markers (``**bold**``/``<font>``/inline code) for the text channel."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", md)
    text = re.sub(r"</?font[^>]*>", "", text)
    return re.sub(r"`([^`]+)`", r"\1", text)


def risk_level(churn_rate: float, threshold: float | None = None) -> Dict[str, object]:
    """Map the predicted churn rate onto a 4-level risk badge (🔴/🟠/🟡/🟢)."""
    thr = float(threshold) if threshold else float(config.ALERT_CHURN_RATE_THRESHOLD or 0.4)
    rate = float(churn_rate or 0.0)
    multiple = (rate / thr) if thr else 0.0
    if multiple >= config.RISK_CRITICAL_MULTIPLIER:
        key, label, color, headline, action = RISK_LEVELS[0]
    elif multiple >= config.RISK_ALERT_MULTIPLIER:
        key, label, color, headline, action = RISK_LEVELS[1]
    elif multiple >= config.RISK_WATCH_MULTIPLIER:
        key, label, color, headline, action = RISK_LEVELS[2]
    else:
        key, label, color, headline, action = RISK_LEVELS[3]
    return {"key": key, "label": label, "color": color, "headline": headline,
            "action": action, "multiple": round(multiple, 2), "threshold": thr,
            "churn_rate": rate, "must_act": key in ("critical", "alert")}


def collect_run_status(db_path: str | Path | None = None) -> Dict[str, object]:
    """Latest status of every pipeline stage + the last daily-run error (if any).

    Two sources, newest wins:
      * the ``pipeline_runs`` table — written by every stage (``db.record_run``), so it also
        works for a bare ``scripts/run_pipeline.py`` invocation;
      * ``reports/last_run.json`` — written by ``scripts/daily_run.py``, the only place that
        knows the *overall* outcome plus the traceback and the wall-clock duration.
    """
    import sqlite3

    status: Dict[str, object] = {"ok": None, "source": "unknown", "ran_at": "", "elapsed_s": None,
                                 "stages": [], "failed": [], "error": "", "n_total": 0, "n_ok": 0}
    path = Path(db_path or config.DB_PATH)
    if path.exists():
        try:
            conn = sqlite3.connect(path)
            try:
                rows = conn.execute(
                    "SELECT stage, status, detail, ran_at FROM pipeline_runs "
                    "WHERE id IN (SELECT MAX(id) FROM pipeline_runs GROUP BY stage) ORDER BY id"
                ).fetchall()
            finally:
                conn.close()
            status["stages"] = [{"stage": r[0], "status": r[1], "detail": r[2] or "",
                                 "ran_at": r[3] or ""} for r in rows]
            status["failed"] = [s for s in status["stages"] if s["status"] != "ok"]
            if rows:
                status["source"] = "pipeline_runs"
                status["ran_at"] = max((r[3] or "") for r in rows)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("读取 pipeline_runs 失败: %s", exc)

    meta_path = config.REPORTS_DIR / "last_run.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            status["source"] = "last_run.json"
            status["ran_at"] = (meta.get("finished_at") or meta.get("generated_at")
                                or status["ran_at"])
            status["elapsed_s"] = meta.get("elapsed_s")
            status["error"] = (meta.get("error") or "").strip()
            if meta.get("ok") is not None:
                status["ok"] = bool(meta["ok"])
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("读取 %s 失败: %s", meta_path.name, exc)

    stages = status["stages"] or []
    if status["ok"] is None:
        status["ok"] = all(s["status"] == "ok" for s in stages) if stages else None
    status["n_total"] = len(stages)
    status["n_ok"] = sum(1 for s in stages if s["status"] == "ok")
    return status


def render_run_status_md(status: Dict[str, object]) -> str:
    """Markdown (``lark_md``) block for the run status + error details."""
    ok = status.get("ok")
    if ok is None and not status.get("stages"):
        head = "**运行状态**：❓ 未找到运行记录（请先跑一次流水线）"
    else:
        head = "**运行状态**：" + ("✅ 成功" if ok else "❌ 失败")
        if status.get("n_total"):
            head += f"（{status['n_ok']}/{status['n_total']} 阶段成功）"
    if status.get("ran_at"):
        head += f"\n**完成时间**：{status['ran_at']}"
    if status.get("elapsed_s"):
        head += f" ｜ **耗时**：{status['elapsed_s']} 秒"
    lines = [head]
    for s in status.get("stages") or []:
        mark = "✅" if s["status"] == "ok" else "❌"
        name = STAGE_NAMES.get(s["stage"] or "", s["stage"] or "-")
        lines.append(f"- {mark} {name} · {s['detail'] or '（无明细）'}")
    if status.get("error"):
        err = str(status["error"]).strip().splitlines()[-1][:200]
        lines.append(f"\n**报错内容**：<font color='red'>{err}</font>")
    elif status.get("failed"):
        names = "、".join(STAGE_NAMES.get(f["stage"] or "", f["stage"] or "-")
                         for f in status["failed"])
        lines.append(f"\n**报错内容**：<font color='red'>{names} 失败</font>")
    else:
        lines.append("\n**报错内容**：无")
    return "\n".join(lines)


def render_run_status_html(status: Dict[str, object]) -> str:
    """HTML twin of :func:`render_run_status_md`（错误文本转义，供邮件模板使用）。"""
    ok = status.get("ok")
    if ok is None and not status.get("stages"):
        head = "❓ 未找到运行记录（请先跑一次流水线）"
    else:
        head = "✅ 成功" if ok else "❌ 失败"
        if status.get("n_total"):
            head += f"（{status['n_ok']}/{status['n_total']} 阶段成功）"
    if status.get("ran_at"):
        head += f" ｜ 完成时间：{html.escape(str(status['ran_at']))}"
    if status.get("elapsed_s"):
        head += f" ｜ 耗时：{html.escape(str(status['elapsed_s']))} 秒"
    lines = [f"<b>运行状态</b>：{head}"]
    for s in status.get("stages") or []:
        mark = "✅" if s["status"] == "ok" else "❌"
        name = STAGE_NAMES.get(s["stage"] or "", s["stage"] or "-")
        lines.append(f"{mark} {html.escape(name)} · {html.escape(str(s['detail'] or '（无明细）'))}")
    if status.get("error"):
        err = str(status["error"]).strip().splitlines()[-1][:200]
        lines.append(f"<b>报错内容</b>：<span style='color:#dc2626'>{html.escape(err)}</span>")
    else:
        lines.append("<b>报错内容</b>：无")
    return "<br>".join(lines)


def horizon_rows(summary: Dict[str, float] | None) -> List[Dict[str, object]]:
    """Pull the multi-window forecasts out of the summary (fallback: the main window only)."""
    summary = summary or {}
    forecasts = summary.get("horizon_forecasts") or {}
    rows = [dict(forecasts[k]) for k in sorted(forecasts, key=lambda k: int(k))]
    if not rows and summary.get("forecast_churn_rate") is not None:
        rows.append({
            "horizon": summary.get("horizon", config.FORECAST_HORIZON_DAYS),
            "avg_daily_freq": summary.get("forecast_mean_freq", "-"),
            "freq_drop": summary.get("avg_freq_drop", "-"),
            "churn_rate": float(summary.get("forecast_churn_rate") or 0.0),
            "final_day_churn_rate": summary.get("final_day_churn_rate"),
        })
    return rows


def render_horizon_md(rows: List[Dict[str, object]], threshold: float) -> str:
    """Markdown block for the 1 / 7 / 14 / 30 / 90-day churn-rate forecasts."""
    if not rows:
        return "**多时间窗流失率预测**：暂无（请先跑完 ④ 流失预测）"
    lines = [f"**多时间窗流失率预测**（越线标 ⚠️，阈值 {threshold:.0%}）"]
    for r in rows:
        rate = float(r.get("churn_rate") or 0.0)
        mark = " ⚠️" if rate >= threshold else ""
        lines.append(
            f"- **{int(r.get('horizon', 0))} 天**：流失率 **{rate:.1%}**{mark}"
            f" ｜ 人均日频 {_num(r.get('avg_daily_freq'), 2)}"
            f"（较当前降 {_num(r.get('freq_drop'), 2)}）"
        )
    if len(rows) > 1:
        short = float(rows[0].get("churn_rate") or 0.0)
        long_row = max(rows, key=lambda r: int(r.get("horizon", 0)))
        long_rate = float(long_row.get("churn_rate") or 0.0)
        if short >= max(long_rate, 1e-9):
            lines.append(f"- 结论：**短窗（{int(rows[0].get('horizon', 0))} 天）流失率已不低于长窗**，"
                         "说明正在**加速出逃**，需立刻干预。")
        else:
            lines.append(f"- 结论：短窗低于长窗，多数地址是**渐进式失活**"
                         f"（{int(long_row.get('horizon', 0))} 天累积 {long_rate:.1%}），"
                         "仍有挽回窗口。")
    return "\n".join(lines)


def render_horizon_text(rows: List[Dict[str, object]], threshold: float) -> List[str]:
    """Plain-text twin of :func:`render_horizon_md` for the non-card fallback."""
    return _md_bold_to_text(render_horizon_md(rows, threshold)).splitlines()


def build_suggestions(summary: Dict[str, float] | None, digest: Dict[str, object],
                      status: Dict[str, object], risk: Dict[str, object],
                      horizons: List[Dict[str, object]] | None = None) -> List[str]:
    """Template-driven「初步推进建议」: 数据驱动的行动 + 该拉哪些 stakeholder。

    Deliberately rule-based (no LLM): a deterministic, auditable suggestion list that costs
    nothing to run every day. See README「下一步优化」for the LLM-based extension and why it
    stays optional.
    """
    summary = summary or {}
    horizons = horizons if horizons is not None else horizon_rows(summary)
    tips: List[str] = []

    if status.get("ok") is False or status.get("failed"):
        bad = "、".join(STAGE_NAMES.get(f["stage"] or "", f["stage"] or "-")
                       for f in (status.get("failed") or [])) or "流水线"
        tips.append(f"**先修数据管道**：{bad} 失败，先保证数据可信再谈结论"
                    "（责任方：**数据/平台工程** ｜ 依据：运行状态 ❌）")

    if len(horizons) > 1:
        short = float(horizons[0].get("churn_rate") or 0.0)
        long_row = max(horizons, key=lambda r: int(r.get("horizon", 0)))
        if short >= max(float(long_row.get("churn_rate") or 0.0), 1e-9):
            tips.append("**立刻止血**：短窗流失率已高于长窗，属于加速出逃，"
                        "建议 48h 内上线一轮定向召回"
                        "（责任方：**增长/运营 + 客户成功** ｜ 依据：多时间窗对比）")

    rows = digest.get("rows") or []
    risky = [r for r in rows if r["label"] in RISK_LABELS]
    if risky:
        biggest = max(risky, key=lambda r: int(r["n"]))
        act, owner, why = CLUSTER_ACTIONS.get(biggest["label"], ("专项分析", "数据", "行为异常"))
        tips.append(f"**主攻「{biggest['label']}」**（{biggest['n']} 个 · 占 {biggest['pct']:.1%}）："
                    f"{act}（责任方：**{owner}** ｜ 依据：{why}）")

    alive = [r for r in rows if r["label"] not in RISK_LABELS]
    if alive:
        churniest = max(alive, key=lambda r: float(r.get("churn_rate") or 0.0))
        act, owner, why = CLUSTER_ACTIONS.get(churniest["label"], ("专项分析", "数据", "行为异常"))
        tips.append(f"**守住基本盘**：「{churniest['label']}」实际流失率最高"
                    f"（{_pct(churniest.get('churn_rate'))}），{act}"
                    f"（责任方：**{owner}** ｜ 依据：{why}）")

    if risk.get("must_act"):
        tips.append(f"**升级报备**：当前 {risk['label']}（{risk['multiple']}× 阈值）→ {risk['action']}；"
                    "建议 24h 内拉齐 增长/产品/风控/数据 开 30 分钟对齐会"
                    "（责任方：**业务负责人 + 数据** ｜ 依据：风险等级）")

    n_addr = digest.get("n_addresses") or 0
    if n_addr:
        tips.append(f"**扩样本 + 拉长时间窗**：本轮 {n_addr} 个地址是**链路可行性验证**"
                    "（全量约 12000 个节点，本机跑不动）→ 申请公司内部 Etherscan API / 归档节点"
                    "跑全量 6 个月窗口，并让 `daily_snapshots` 持续累积"
                    "（责任方：**数据/平台工程 + 管理层** ｜ 依据：样本量与数据资产）")
    return tips


def render_suggestions_md(tips: List[str]) -> str:
    """Markdown block for the suggestion list (the label makes the provenance explicit)."""
    if not tips:
        return "**初步推进建议**：暂无（数据不足）"
    lines = ["**初步推进建议**（模板规则生成，非大模型）"]
    lines += [f"- {t}" for t in tips]
    return "\n".join(lines)


def build_digest_text(summary: Dict[str, float] | None = None,
                      risk_cluster_ratio: float = 0.0,
                      digest: Dict[str, object] | None = None,
                      insights: str | None = None,
                      status: Dict[str, object] | None = None) -> str:
    """Compose the plain-text daily digest (fallback channel + e-mail/preview friendly).

    板块顺序（要求：运行状态/报错、高危等级、多时间窗放最前）：
      运行状态 → 风险等级 → 多时间窗预测 → 一、流失预警 → 二、聚类结果 →
      三、业务洞察 → 初步推进建议 → 看板链接
    """
    summary = summary or {}
    digest = digest if digest is not None else collect_cluster_digest()
    status = status if status is not None else collect_run_status()
    triggered, reasons, context = evaluate_alerts(summary, risk_cluster_ratio,
                                                  status=status, digest=digest)
    risk = risk_level(summary.get("forecast_churn_rate") or 0.0)
    rows = horizon_rows(summary)
    horizon = summary.get("horizon", config.FORECAST_HORIZON_DAYS)
    rule = "————————————"
    return "\n".join([
        "📮 加密用户行为聚类 & 流失预测 · 每日简报",
        f"{context['generated_at']} ｜ 预测时间窗 {horizon} 天 ｜ 风险等级 {risk['label']}",
        rule,
        _md_bold_to_text(render_run_status_md(status)),
        rule,
        f"{risk['label']} ｜ {risk['headline']}（{risk['multiple']}× 阈值 "
        f"{risk['threshold']:.0%}）→ {risk['action']}",
        *render_horizon_text(rows, risk["threshold"]),
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
        _md_bold_to_text(render_suggestions_md(
            build_suggestions(summary, digest, status, risk, horizons=rows))),
        rule,
        f"看板: {config.DASHBOARD_URL}",
    ])


def render_cluster_md(digest: Dict[str, object]) -> str:
    """Markdown twin of :func:`render_cluster_text`（簇名加粗、高危簇加 ⚠️）。"""
    rows = digest.get("rows") or []
    if not rows:
        return "暂无可用的聚类数据（请先跑完聚类阶段）。"
    lines = [
        f"HDBSCAN · **{digest['n_personas']}** 类 + **{digest['n_noise']}** 个噪音点"
        f" · 覆盖 **{digest['n_addresses']}** 个地址"
    ]
    for r in rows:
        mark = "⚠️ " if r["label"] in RISK_LABELS else ""
        lines.append(
            f"{mark}**{r['label']}** · {r['n']} 个（{r['pct']:.1%}）"
            f" ｜ 实际流失率 **{_pct(r['churn_rate'])}**"
            f" · 日均 {_num(r['freq'], 2)} 笔 · 持仓 {_num(r['holding'], 1)}h"
            f" · 协议 {_num(r['protocols'], 1)} 个 · 闲置 {_num(r['idle_days'], 1)} 天"
        )
        note = PERSONA_NOTES.get(r["label"])
        if note:
            lines.append(f"　└ {note}")
    lines.append(f"高危行为簇占比 **{digest['risk_ratio']:.1%}**"
                 f"（{'、'.join(sorted(RISK_LABELS))}）")
    return "\n".join(lines)


def _lark_at_prefix() -> str:
    """``lark_md`` @-mention prefix (needs a real open_id; plain phone numbers are ignored)."""
    target = (config.LARK_AT_ID or "").strip()
    if target.startswith(("ou_", "on_")):
        return f"<at id={target}></at>\n"
    if target == "all":
        return "<at id=all></at>\n"
    return ""


def build_digest_card(summary: Dict[str, float] | None = None,
                       risk_cluster_ratio: float = 0.0,
                       digest: Dict[str, object] | None = None,
                       insights: str | None = None,
                       status: Dict[str, object] | None = None) -> Dict[str, object]:
    """Interactive **markdown** card for the Lark daily digest.

    Answering「只能是 plain text 吗」: **不是**。Lark 群机器人 webhook 支持
    ``msg_type: "interactive"``（消息卡片），卡片内文本用 ``lark_md`` 方言即可获得真
    **加粗**、彩色标题栏、分割线、超链接与 @；另一种 ``msg_type: "post"``（富文本）也能
    加粗，但卡片多了彩色标题与分割线，因此简报用卡片。
    """
    summary = summary or {}
    digest = digest if digest is not None else collect_cluster_digest()
    status = status if status is not None else collect_run_status()
    triggered, reasons, context = evaluate_alerts(summary, risk_cluster_ratio,
                                                  status=status, digest=digest)
    risk = risk_level(summary.get("forecast_churn_rate") or 0.0)
    rows = horizon_rows(summary)
    horizon = summary.get("horizon", config.FORECAST_HORIZON_DAYS)
    tips = build_suggestions(summary, digest, status, risk, horizons=rows)
    insight_text = insights if insights is not None else read_insights()

    if status.get("ok") is False or risk["key"] == "critical":
        color = "red"          # 运行失败 / 🔴 高危 → 红头
    else:
        color = risk["color"]  # 🟠 橙 / 🟡 黄 / 🟢 绿
    elements: List[Dict[str, object]] = [
        {"tag": "div", "text": {"tag": "lark_md",
                                "content": _lark_at_prefix() + render_run_status_md(status)}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": (
            f"**{risk['label']}** ｜ **{risk['headline']}**"
            f"（{risk['multiple']}× 阈值 {risk['threshold']:.0%}）\n"
            f"**处置建议**：{risk['action']}"
            + ("\n<font color='red'>**结论：当前流失率属于必须重视的等级，"
               "建议立刻进入处置流程。**</font>" if risk["must_act"] else ""))}},
        {"tag": "div", "text": {"tag": "lark_md",
                                "content": render_horizon_md(rows, risk["threshold"])}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join([
            "**一、流失预警**（" + ("已触发 ⚠️" if triggered else "未达阈值，仅例行播报") + "）",
            f"- 预测流失率（{horizon} 天）：**{context['churn_rate']}**"
            f"（阈值 {context['churn_rate_threshold']}）",
            f"- 高危流失占比：**{context['high_risk_ratio']}**"
            f"（阈值 {context['risk_ratio_threshold']}）"
            f" ｜ 高危行为簇占比：**{risk_cluster_ratio:.1%}**",
            f"- 人均日交易频次：{summary.get('current_mean_freq', '-')} → 第 {horizon} 天 "
            f"{summary.get('final_day_freq', '-')}（降幅 **{context['freq_drop']}**）",
            "- 触发原因：" + ("；".join(reasons) if reasons else "未达阈值，仅例行播报"),
        ])}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md",
                                "content": "**二、用户行为聚类结果**\n" + render_cluster_md(digest)}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": (
            "**三、业务洞察**（从聚类与模型自动提炼）\n"
            + (insight_text or "（暂无 docs/insights.md，请先运行 churn_model）"))}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": render_suggestions_md(tips)}},
        {"tag": "note", "elements": [
            {"tag": "lark_md", "content": f"[打开实时看板 →]({config.DASHBOARD_URL})"
                                          " ｜ 由 `src/notify.py` 自动生成（模板规则，非大模型）"}]},
    ]
    return {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "template": color,
            "title": {"tag": "plain_text", "content": "📮 加密用户行为聚类 & 流失预测 · 每日简报"},
            "subtitle": {"tag": "plain_text",
                         "content": f"{context['generated_at']} ｜ 时间窗 {horizon} 天 ｜ "
                                    f"{risk['label']}（{risk['multiple']}× 阈值）"},
        },
        "elements": elements,
    }


# --- decision + dispatch ------------------------------------------------------
def evaluate_alerts(summary: Dict[str, float], risk_cluster_ratio: float = 0.0,
                    status: Dict[str, object] | None = None,
                    digest: Dict[str, object] | None = None):
    """Return (triggered, reasons, context) based on the configured thresholds.

    ``context`` feeds the HTML e-mail template, so it also carries the run status, the risk
    badge, the multi-window forecast table and the template-driven suggestions.
    """
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
    risk = risk_level(churn_rate)
    status = status if status is not None else collect_run_status()
    rows = horizon_rows(summary)
    tips = build_suggestions(summary, digest if digest is not None else collect_cluster_digest(),
                             status, risk, horizons=rows)

    horizon_detail = [
        (f"预测流失率（{r['horizon']} 天）", f"{float(r.get('churn_rate') or 0):.2%}")
        for r in rows
    ]
    ctx_rows = [
        ("运行状态", "✅ 成功" if status.get("ok") else
         ("❌ 失败" if status.get("ok") is False else "❓ 未知")),
        ("风险等级", f"{risk['label']}（{risk['multiple']}× 阈值，"
                     f"{'必须重视' if risk['must_act'] else '暂不强制处置'}）"),
    ] + horizon_detail + [
        ("高危地址占比", f"{high_risk:.2%}"),
        ("当前人均日交易频次", f"{summary.get('current_mean_freq', '-')}"),
        (f"第 {summary.get('horizon', config.FORECAST_HORIZON_DAYS)} 天人均日交易频次",
         f"{summary.get('final_day_freq', '-')}"),
        ("人均频次降低值", f"{freq_drop:.3f}"),
        ("预测窗口", f"{summary.get('horizon', config.FORECAST_HORIZON_DAYS)} 天"),
    ]

    context = {
        "alert_level": f"{risk['label']} · {risk['headline']}",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "churn_rate": f"{churn_rate:.1%}",
        "churn_rate_threshold": f"{config.ALERT_CHURN_RATE_THRESHOLD:.0%}",
        "high_risk_ratio": f"{high_risk:.1%}",
        "risk_ratio_threshold": f"{config.ALERT_RISK_CLUSTER_RATIO:.0%}",
        "freq_drop": f"{freq_drop:.3f}",
        "trigger_reasons": "<br>".join(f"• {r}" for r in reasons) or "• 未触发（仅例行通知）",
        "dashboard_url": config.DASHBOARD_URL,
        # —— 本次新增：运行状态 / 风险等级 / 多时间窗 / 推进建议 ——
        "run_status_text": render_run_status_html(status),
        "risk_level_text": (f"{risk['label']} ｜ {risk['headline']}"
                            f"（{risk['multiple']}× 阈值 {risk['threshold']:.0%}）"
                            f"→ {risk['action']}"),
        "horizon_table_rows": _detail_rows(horizon_detail),
        "suggestions_html": "<br>".join(f"• {html.escape(_md_bold_to_text(t))}" for t in tips)
                            or "• 暂无（数据不足）",
        "detail_rows": _detail_rows(ctx_rows),
    }
    return triggered, reasons, context


def send_alerts(summary: Dict[str, float], risk_cluster_ratio: float = 0.0,
                force: bool = False, digest: Dict[str, object] | None = None,
                status: Dict[str, object] | None = None) -> bool:
    """Render + dispatch e-mail & Lark alert (and a clickable macOS banner)."""
    status = status if status is not None else collect_run_status()
    triggered, reasons, context = evaluate_alerts(summary, risk_cluster_ratio,
                                                  status=status, digest=digest)
    if not triggered and not force:
        logger.info("未达到预警阈值，跳过告警发送")
        return False

    subject = f"⚠️ 加密用户流失预警 · 流失率 {context['churn_rate']} · 高危占比 {context['high_risk_ratio']}"
    html_body = render_email(context)
    email_ok = send_email(subject, html_body)

    # Lark gets the full picture: run status + risk level + multi-window + clustering + insights.
    lark_ok = _send_lark_digest_payload(summary, risk_cluster_ratio, digest=digest, status=status)
    risk = risk_level(summary.get("forecast_churn_rate") or 0.0)
    notify_macos(
        f"运行{'成功' if status.get('ok') else '失败'} ｜ 流失率 {context['churn_rate']} "
        f"｜ {risk['label']} ｜ 点开看 邮件/Lark/看板",
        title="⚠️ 加密用户流失预警",
        url=str(config.STATUS_PAGE if Path(config.STATUS_PAGE).exists() else config.DASHBOARD_URL))
    logger.info("告警发送: email=%s, lark=%s", email_ok, lark_ok)
    return email_ok or lark_ok


def _send_lark_digest_payload(summary: Dict[str, float] | None,
                              risk_cluster_ratio: float,
                              digest: Dict[str, object] | None = None,
                              status: Dict[str, object] | None = None) -> bool:
    """Push the digest as a markdown **card**; fall back to plain text if the card fails."""
    digest = digest if digest is not None else collect_cluster_digest()
    status = status if status is not None else collect_run_status()
    card = build_digest_card(summary, risk_cluster_ratio, digest=digest, status=status)
    if send_lark_card(card):
        return True
    logger.warning("Lark 卡片推送失败，回退为纯文本简报")
    return send_lark(build_digest_text(summary, risk_cluster_ratio, digest=digest, status=status))


def send_lark_digest(summary: Dict[str, float] | None = None,
                     risk_cluster_ratio: float | None = None,
                     digest: Dict[str, object] | None = None,
                     status: Dict[str, object] | None = None) -> bool:
    """Always-on daily push: markdown card with status + clustering results + insights.

    Unlike :func:`send_alerts` this is *not* gated by the churn thresholds — the group
    should receive the clustering picture every day; a crossed threshold merely turns the
    warning line red (⚠️) and flips the 风险等级 badge.
    """
    digest = digest if digest is not None else collect_cluster_digest()
    if risk_cluster_ratio is None:
        risk_cluster_ratio = float(digest.get("risk_ratio") or 0.0)
    return _send_lark_digest_payload(summary, risk_cluster_ratio, digest=digest, status=status)


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

    ``python -m src.notify --preview``      → render only (no network): writes the e-mail HTML
                                              and the Lark card JSON under ``reports/``
    ``python -m src.notify --oauth-login`` → one-off Google consent, prints the refresh token
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--oauth-login" in argv:
        return oauth_login()

    summary = _report_summary()
    digest = collect_cluster_digest()
    risk_ratio = float(digest.get("risk_ratio") or 0.0)
    status = collect_run_status()
    _, _, context = evaluate_alerts(summary, risk_ratio, status=status, digest=digest)

    if "--preview" in argv:
        # 只渲染、不发送：把邮件 HTML 与 Lark 卡片落盘，便于离线核对（不消耗配额、不需代理）
        config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        html_path = config.REPORTS_DIR / config.artefact("email_preview.html")
        card_path = config.REPORTS_DIR / config.artefact("lark_card.json")
        html_path.write_text(render_email(context), encoding="utf-8")
        card_path.write_text(
            json.dumps(build_digest_card(summary, risk_ratio, digest=digest, status=status),
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(build_digest_text(summary, risk_ratio, digest=digest, status=status))
        logger.info("预览已生成（未发送）: %s / %s", html_path, card_path)
        return 0

    subject = (f"[测试] ⚠️ 加密用户流失预警 · 流失率 {context['churn_rate']}"
               f" · 高危占比 {context['high_risk_ratio']}")
    email_ok = send_email(subject, render_email(context))
    lark_ok = _send_lark_digest_payload(summary, risk_ratio, digest=digest, status=status)
    print(build_digest_text(summary, risk_ratio, digest=digest, status=status))
    logger.info("测试推送结果: email=%s, lark=%s", email_ok, lark_ok)
    return 0 if (email_ok or lark_ok) else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
