"""Local run-status page (``reports/status.html``).

Why: the macOS banner is only a one-liner. Clicking the banner (``terminal-notifier -open``)
must land on a page that shows

  1. **运行状态 + 报错内容**（每个阶段 ok/fail、整体成败、耗时）
  2. **三个方向的入口与结果**：邮件 / Lark 简报 / Streamlit 看板
  3. **结果内容**：风险等级（是否高危）、1/7/14/30/90 天多时间窗流失率、
     聚类画像、洞察、初步推进建议

Usage
-----
python -m src.status_page            # 用最近一次运行结果重新生成并打印路径
"""
from __future__ import annotations

import datetime as dt
import html
import logging
import sys
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, notify  # noqa: E402

logger = logging.getLogger("churn.status_page")

RISK_BADGE = {"critical": "#dc2626", "alert": "#ea580c", "watch": "#ca8a04", "normal": "#16a34a"}


def _row(label: str, value: str) -> str:
    return f"<tr><th>{html.escape(str(label))}</th><td>{value}</td></tr>"


def _cluster_rows(digest: Dict[str, object]) -> str:
    rows = digest.get("rows") or []
    if not rows:
        return "<tr><td colspan='6'>暂无可用的聚类数据</td></tr>"
    out = []
    for r in rows:
        flag = " ⚠️" if r["label"] in notify.RISK_LABELS else ""
        out.append(
            "<tr>"
            f"<td><b>{html.escape(str(r['label']))}{flag}</b></td>"
            f"<td>{r['n']}（{r['pct']:.1%}）</td>"
            f"<td>{r['churn_rate']:.1%}</td>"
            f"<td>{notify._num(r['freq'], 2)}</td>"
            f"<td>{notify._num(r['holding'], 1)}h</td>"
            f"<td>{notify._num(r['idle_days'], 1)} 天</td>"
            "</tr>")
    return "".join(out)


def build_status_page(summary: Dict[str, float] | None = None,
                      digest: Dict[str, object] | None = None,
                      status: Dict[str, object] | None = None,
                      email_ok: bool | None = None,
                      lark_ok: bool | None = None,
                      report_path: Path | str | None = None,
                      dashboard_url: str | None = None,
                      n_transactions: int | None = None,
                      n_addresses: int | None = None) -> Path:
    """Render the run-status page and return its path (``config.STATUS_PAGE``)."""
    summary = summary or {}
    digest = digest if digest is not None else notify.collect_cluster_digest()
    status = status if status is not None else notify.collect_run_status()
    risk = notify.risk_level(summary.get("forecast_churn_rate") or 0.0)
    rows = notify.horizon_rows(summary)
    run_ok = status.get("ok")
    badge = "✅ 成功" if run_ok else ("❌ 失败" if run_ok is False else "❓ 未知")
    badge_color = "#16a34a" if run_ok else ("#dc2626" if run_ok is False else "#6b7280")

    horizon_rows_html = "".join(
        _row(f"预测流失率（{int(r.get('horizon', 0))} 天）",
             f"{float(r.get('churn_rate') or 0):.1%}"
             + ("　⚠️ 越线" if float(r.get("churn_rate") or 0) >= risk["threshold"] else ""))
        for r in rows) or _row("多时间窗预测", "暂无")

    report_link = ""
    if report_path and Path(report_path).exists():
        report_link = f' ｜ <a href="file://{html.escape(str(report_path))}">打开 HTML 日报 →</a>'
    dash = dashboard_url or config.DASHBOARD_URL

    def _file_link(rel_name: str, label: str) -> str:
        p = config.REPORTS_DIR / config.artefact(rel_name)
        if not p.exists():
            return ""
        return f' ｜ <a href="file://{html.escape(str(p))}">{label} →</a>'

    email_extra = _file_link("email_preview.html", "邮件 HTML 预览")
    lark_extra = _file_link("lark_card.json", "Lark 卡片 JSON 原文")
    lark_text = notify.build_digest_text(summary, float(digest.get("risk_ratio") or 0.0),
                                        digest=digest, status=status)
    suggestions = "".join(
        f"<li>{html.escape(notify._md_bold_to_text(t))}</li>"
        for t in notify.build_suggestions(summary, digest, status, risk, horizons=rows))

    ctx = {
        "badge": badge, "badge_color": badge_color, "risk": risk, "summary": summary,
        "status": status, "digest": digest, "horizon_rows_html": horizon_rows_html,
        "suggestions": suggestions, "lark_text": lark_text, "dash": dash,
        "report_link": report_link, "email_ok": email_ok, "lark_ok": lark_ok,
        "email_extra": email_extra, "lark_extra": lark_extra,
        "n_transactions": n_transactions, "n_addresses": n_addresses,
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    page = _render_page(ctx)
    path = Path(config.STATUS_PAGE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    logger.info("运行状态页已生成 -> %s", path)
    return path

def _render_page(ctx: Dict[str, object]) -> str:
    """Build the HTML document from the prepared context (kept separate for readability)."""
    risk = ctx["risk"]
    summary = ctx["summary"]
    status = ctx["status"]
    digest = ctx["digest"]
    horizon = summary.get("horizon", config.FORECAST_HORIZON_DAYS)
    sample_addr = ctx["n_addresses"] if ctx["n_addresses"] is not None else digest.get("n_addresses", "-")
    sample_tx = ctx["n_transactions"] if ctx["n_transactions"] is not None else "-"
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>加密用户行为聚类 &amp; 流失预测 · 运行状态</title>
<style>
 :root{{color-scheme:light;}}
 body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
   background:#f4f6fb;color:#0f172a;margin:0;padding:28px;}}
 .wrap{{max-width:940px;margin:auto;}}
 .card{{background:#fff;border-radius:16px;padding:26px 28px;margin-bottom:18px;
   box-shadow:0 6px 24px rgba(15,23,42,.08);}}
 h1{{font-size:22px;margin:0 0 6px;}} h2{{font-size:16px;margin:0 0 12px;color:#334155;}}
 .badge{{display:inline-block;padding:4px 12px;border-radius:999px;color:#fff;
   background:{ctx['badge_color']};font-weight:600;}}
 .risk{{display:inline-block;padding:4px 12px;border-radius:999px;color:#fff;
   background:{RISK_BADGE.get(risk['key'], '#6b7280')};font-weight:600;}}
 .meta{{color:#64748b;font-size:13px;margin-top:8px;}}
 table{{border-collapse:collapse;width:100%;margin-top:10px;font-size:14px;}}
 th,td{{border:1px solid #e5e7eb;padding:8px 12px;text-align:left;vertical-align:top;}}
 th{{background:#f3f4f6;width:230px;}}
 a{{color:#4f46e5;text-decoration:none;}} a:hover{{text-decoration:underline;}}
 pre{{background:#0f172a;color:#e2e8f0;padding:14px;border-radius:10px;overflow:auto;font-size:12px;}}
 details summary{{cursor:pointer;color:#4f46e5;}}
 ul{{margin:8px 0 0 18px;}} li{{margin:4px 0;line-height:1.55;}}
 .note{{color:#64748b;font-size:12px;line-height:1.6;}}
</style></head><body><div class="wrap">

<div class="card">
  <h1>📮 加密用户行为聚类 &amp; 流失预测 · 运行状态</h1>
  <p>运行状态：<span class="badge">{ctx['badge']}</span>
     &nbsp;风险等级：<span class="risk">{html.escape(str(risk['label']))}</span>
     &nbsp;{html.escape(str(risk['headline']))}（{risk['multiple']}× 阈值）</p>
  <p class="meta">生成时间：{ctx['generated_at']}
     ｜ 最近完成：{html.escape(str(status.get('ran_at') or '-'))}
     ｜ 耗时：{html.escape(str(status.get('elapsed_s') or '-'))} 秒
     ｜ 预测时间窗：{horizon} 天</p>
</div>

<div class="card">
  <h2>一、运行状态 + 报错内容</h2>
  <table><tr><td>{notify.render_run_status_html(status)}</td></tr></table>
</div>

<div class="card">
  <h2>二、三个方向的入口与结果</h2>
  <table>
    <tr><th>📧 邮件</th><td>{"✅ 已发送（预警）" if ctx['email_ok'] else "— 未触发/未发送"}
       ｜ 收件人：{html.escape(str(getattr(config, "ALERT_EMAIL_TO", "") or ""))}{ctx['report_link']}{ctx['email_extra']}</td></tr>
    <tr><th>💬 Lark / 飞书</th><td>{"✅ 已推送（卡片）" if ctx['lark_ok'] else "— 未推送"}
       ｜ 原文见末尾折叠块{ctx['lark_extra']}</td></tr>
    <tr><th>📊 Streamlit 看板</th><td><a href="{html.escape(ctx['dash'])}" target="_blank">{html.escape(ctx['dash'])}</a>
       （16 panels：聚类 / 模型 / 预测 / 数据资产）</td></tr>
  </table>
</div>

<div class="card">
  <h2>三、结果内容</h2>
  <table>
    {_row("预测流失率（主窗口）", f"{float(summary.get('forecast_churn_rate') or 0):.1%}")}
    {ctx['horizon_rows_html']}
    {_row("高危地址占比", f"{float(summary.get('high_risk_ratio') or 0):.1%}")}
    {_row("高危行为簇占比", f"{float(digest.get('risk_ratio') or 0):.1%}")}
    {_row("当前人均日交易频次", str(summary.get("current_mean_freq", "-")))}
    {_row(f"第 {horizon} 天人均日交易频次", str(summary.get("final_day_freq", "-")))}
    {_row("人均频次降低值", str(summary.get("avg_freq_drop", "-")))}
    {_row("样本量（地址 / 交易）", f"{sample_addr} / {sample_tx}")}
  </table>
</div>

<div class="card">
  <h2>四、用户行为聚类结果（文字版，与 Lark 一致）</h2>
  <table>
    <tr><th>簇 / 画像</th><th>规模</th><th>实际流失率</th><th>日均笔数</th><th>持仓</th><th>闲置</th></tr>
    {_cluster_rows(digest)}
  </table>
</div>

<div class="card">
  <h2>五、业务洞察</h2>
  <pre style="background:#f8fafc;color:#0f172a;">{html.escape(notify.read_insights())}</pre>
</div>

<div class="card">
  <h2>六、初步推进建议（模板规则生成，非大模型）</h2>
  <ul>{ctx['suggestions'] or '<li>暂无（数据不足）</li>'}</ul>
</div>

<div class="card">
  <h2>附：Lark 推送原文（纯文本兜底格式）</h2>
  <details><summary>展开 / 收起</summary>
    <pre>{html.escape(str(ctx['lark_text']))}</pre>
  </details>
  <p class="note">说明：本轮样本量是本机跑得动的验证规模（约 2000 个地址）；
     全量约 12000 个节点需要公司内部 Etherscan API 或归档节点才能跑完整时间窗，详见 README。</p>
</div>

</div></body></html>"""


def open_status_page(path: Path | str | None = None) -> None:
    """Open the status page in the default browser (used by clickable notifications)."""
    import subprocess

    target = Path(path or config.STATUS_PAGE)
    if sys.platform == "darwin":
        subprocess.run(["open", str(target)], check=False)
    else:  # pragma: no cover
        import webbrowser

        webbrowser.open(f"file://{target}")


def main() -> int:
    """``python -m src.status_page`` → rebuild the page from the latest real results."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    summary_path = config.REPORTS_DIR / config.artefact("churn_summary.json")
    summary: Dict[str, float] = {}
    if summary_path.exists():
        import json

        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8")) \
                .get("forecast_summary") or {}
        except Exception as exc:  # pragma: no cover
            logger.warning("读取 %s 失败: %s", summary_path.name, exc)
    path = build_status_page(summary)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

