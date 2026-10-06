"""Unit tests for the crypto churn pipeline (no network, no heavy models).

Run:  python -m pytest -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import churn_model, feature_engineer, notify  # noqa: E402


def _sample_transactions() -> pd.DataFrame:
    base = 1_700_000_000
    rows = []
    addr = "0xaaa"
    for i in range(10):
        rows.append({
            "address": addr, "tx_hash": f"h{i}", "timestamp": base + i * 86400,
            "from_address": addr if i % 2 else "0xbbb",
            "to_address": "0xbbb" if i % 2 else addr,
            "value_eth": 1.0, "tx_type": "normal", "contract_address": "",
            "token_symbol": "ETH", "gas_used": 21000, "gas_price": 50e9, "is_error": 0,
        })
    # an ERC-20 receive + later send to exercise the holding-time logic
    rows.append({
        "address": addr, "tx_hash": "e1", "timestamp": base,
        "from_address": "0xccc", "to_address": addr, "value_eth": 100.0,
        "tx_type": "erc20", "contract_address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "token_symbol": "USDT", "gas_used": 50000, "gas_price": 40e9, "is_error": 0,
    })
    rows.append({
        "address": addr, "tx_hash": "e2", "timestamp": base + 2 * 86400,
        "from_address": addr, "to_address": "0xccc", "value_eth": 100.0,
        "tx_type": "erc20", "contract_address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "token_symbol": "USDT", "gas_used": 50000, "gas_price": 40e9, "is_error": 0,
    })
    return pd.DataFrame(rows)


def test_feature_engineer_produces_all_columns():
    feats = feature_engineer.compute_features(_sample_transactions())
    assert len(feats) == 1
    for col in feature_engineer_test_columns():
        assert col in feats.columns
    row = feats.iloc[0]
    assert row["total_tx"] == 12
    assert row["tx_freq_daily"] > 0
    assert row["protocol_diversity"] >= 1
    # received at base, sent 2 days later -> ~48h holding
    assert 47 <= row["avg_holding_hours"] <= 49


def feature_engineer_test_columns():
    return [
        "tx_freq_daily", "tx_freq_weekly", "tx_freq_monthly", "avg_holding_hours",
        "high_gas_ratio", "protocol_diversity", "unique_tokens", "token_tx_ratio",
        "eth_balance_end", "eth_balance_trend", "last_tx_days_ago",
    ]


def test_churn_label_uses_threshold():
    df = pd.DataFrame({
        "last_tx_days_ago": [1.0, 31.0, 60.0, 10.0],
        "tx_freq_daily": [1, 0, 0, 2],
    })
    # pad feature columns so build_xy works
    for col in churn_model.MODEL_FEATURES:
        if col not in df:
            df[col] = 0.0
    X, y = churn_model.build_xy(df)
    assert list(y) == [0, 1, 1, 0]


def test_alert_thresholds():
    summary = {"forecast_churn_rate": 0.55, "high_risk_ratio": 0.30, "avg_freq_drop": 1.2}
    triggered, reasons, ctx = notify.evaluate_alerts(summary)
    assert triggered is True
    assert any("流失率" in r for r in reasons)
    assert ctx["churn_rate"].endswith("%")


def test_alert_not_triggered_below_threshold():
    summary = {"forecast_churn_rate": 0.05, "high_risk_ratio": 0.02, "avg_freq_drop": 0.1}
    triggered, _, _ = notify.evaluate_alerts(summary)
    assert triggered is False


def test_email_template_renders_without_leftover_placeholders():
    _, _, ctx = notify.evaluate_alerts(
        {"forecast_churn_rate": 0.4, "high_risk_ratio": 0.2, "avg_freq_drop": 0.5})
    html = notify.render_email(ctx)
    assert "{{" not in html
    assert "40.0%" in html


def test_frequency_vs_churn_buckets():
    df = pd.DataFrame({
        "tx_freq_daily": [0.0, 0.0, 2.0, 3.0, 0.5],
        "last_tx_days_ago": [100, 90, 1, 2, 40],
    })
    agg = churn_model.analyze_frequency_vs_churn(df)
    assert set(agg["freq_bucket"].astype(str)).issuperset({"~0 (≈静止)"})
    assert agg["churn_rate"].max() > 0


def _temp_cluster_db(path: Path) -> Path:
    """Minimal SQLite fixture holding exactly the columns the Lark digest queries."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE address_clusters (address TEXT, cluster_id INTEGER, cluster_label TEXT);
        CREATE TABLE address_features (address TEXT, tx_freq_daily REAL, avg_holding_hours REAL,
            protocol_diversity REAL, high_gas_ratio REAL, eth_balance_end REAL,
            last_tx_days_ago REAL);
        CREATE TABLE churn_predictions (address TEXT, is_churned INTEGER, churn_prob REAL);
    """)
    rows = [
        ("a1", "高频大户", 4.0, 8.0, 3.0, 0.0, 10.0, 70.0, 1),
        ("a2", "高频大户", 2.0, 9.0, 4.0, 0.0, 20.0, 80.0, 0),
        ("a3", "长期持有者", 1.0, 30.0, 5.0, 0.3, -9.0, 12.0, 0),
        ("a4", "高频套利者", 0.4, 28.0, 2.6, 0.9, -6.0, 71.0, 1),
        ("a5", "噪音/机器人", 67.0, 895.0, 73.0, 0.3, 8578.0, 36.0, 0),
    ]
    for addr, label, freq, hold, proto, gas, bal, idle, churned in rows:
        conn.execute("INSERT INTO address_clusters VALUES (?, ?, ?)", (addr, 0, label))
        conn.execute("INSERT INTO address_features VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (addr, freq, hold, proto, gas, bal, idle))
        conn.execute("INSERT INTO churn_predictions VALUES (?, ?, ?)", (addr, churned, 0.5))
    conn.commit()
    conn.close()
    return path


def test_cluster_digest_aggregates_sqlite(tmp_path):
    digest = notify.collect_cluster_digest(_temp_cluster_db(tmp_path / "t.db"))
    assert digest["n_addresses"] == 5
    assert digest["n_personas"] == 3            # 噪音不是 persona
    assert digest["n_noise"] == 1
    assert digest["risk_ratio"] == pytest.approx(2 / 5)   # 噪音 + 高频套利者
    text = notify.render_cluster_text(digest)
    assert "高频大户" in text and "实际流失率 50.0%" in text
    assert "噪音/机器人" in text


def test_cluster_digest_survives_missing_db(tmp_path):
    digest = notify.collect_cluster_digest(tmp_path / "nope.db")
    assert digest["rows"] == [] and digest["n_addresses"] == 0
    assert "暂无可用的聚类数据" in notify.render_cluster_text(digest)


def test_digest_text_carries_warning_clusters_and_insights():
    digest = {"rows": [{"label": "长期持有者", "n": 4, "pct": 1.0, "freq": 1.0, "holding": 30.0,
                        "protocols": 5.0, "gas_ratio": 0.1, "balance": -9.0, "idle_days": 12.0,
                        "churn_rate": 0.1}],
              "n_addresses": 4, "n_personas": 1, "n_noise": 0, "risk_ratio": 0.0}
    text = notify.build_digest_text(
        {"forecast_churn_rate": 0.51, "high_risk_ratio": 0.67, "avg_freq_drop": 14.5,
         "current_mean_freq": 33.8, "final_day_freq": 19.2, "horizon": 30},
        0.0, digest=digest, insights="· 结论\n建议对高危簇提前触达。")
    assert "每日简报" in text
    assert "一、流失预警" in text and "预测流失率（30 天）: 51.0%" in text
    assert "二、【用户行为聚类结果】" in text and "长期持有者" in text
    assert "三、业务洞察" in text and "建议对高危簇提前触达" in text
    assert "http://localhost:8501" in text


def test_lark_payload_only_mentions_a_real_open_id(monkeypatch):
    monkeypatch.setattr(notify.config, "LARK_AT_ID", "13339947334")   # 手机号无法 @
    assert "<at" not in notify._lark_payload("hi")["content"]["text"]
    monkeypatch.setattr(notify.config, "LARK_AT_ID", "ou_abc123")
    payload = notify._lark_payload("hi")
    assert payload["content"]["text"].startswith('<at user_id="ou_abc123"></at>')


def test_read_insights_dedupes_and_strips_noise(tmp_path, monkeypatch):
    (tmp_path / "insights.md").write_text(
        "# 业务洞察 (Insights)\n\n> 本文件由 `src/churn_model.py` 自动生成/更新。\n\n"
        "## 流失预测结论 (自动生成)\n\n### 驱动流失的关键特征 (SHAP)\n"
        "- **total_tx**: 平均 |SHAP| = 1.14\n\n### 结论\n第 30 天预计流失率 51.5%。\n\n"
        "---\n\n### 驱动流失的关键特征 (SHAP)\n- **total_tx**: 平均 |SHAP| = 9.99\n",
        encoding="utf-8")
    monkeypatch.setattr(notify.config, "DOCS_DIR", tmp_path)
    text = notify.read_insights()
    assert "1.14" in text                      # 保留第一份（最新）正文
    assert "9.99" not in text                  # 丢弃历史重复块
    assert "本文件由" not in text and "**" not in text
    assert "结论" in text


def _fake_status(ok: bool = True, error: str = "") -> dict:
    """Minimal stand-in for ``notify.collect_run_status()`` output (no DB touch)."""
    failed = [] if ok else [{"stage": "churn_model", "status": "failed",
                             "detail": "AUC 0.90", "ran_at": ""}]
    return {
        "ok": ok, "source": "last_run.json", "ran_at": "2026-10-06 21:00:00",
        "elapsed_s": 123.4, "error": error, "failed": failed,
        "stages": [
            {"stage": "data_fetcher", "status": "ok", "detail": "2000 地址", "ran_at": ""},
            {"stage": "churn_model", "status": "failed" if not ok else "ok",
             "detail": "AUC 0.90", "ran_at": ""},
        ],
        "n_total": 2, "n_ok": 1 if not ok else 2,
    }


def test_risk_level_buckets():
    assert notify.risk_level(0.70)["key"] == "critical"   # 1.75× 阈值
    assert notify.risk_level(0.45)["key"] == "alert"
    assert notify.risk_level(0.32)["key"] == "watch"
    assert notify.risk_level(0.10)["key"] == "normal"
    assert notify.risk_level(0.70)["must_act"] is True
    assert notify.risk_level(0.10)["must_act"] is False


def test_horizon_rows_and_markdown():
    single = notify.horizon_rows({"forecast_churn_rate": 0.5, "horizon": 30,
                                  "forecast_mean_freq": 3.2, "avg_freq_drop": 0.4})
    assert len(single) == 1 and single[0]["horizon"] == 30
    rows = notify.horizon_rows({"horizon_forecasts": {
        "1": {"horizon": 1, "churn_rate": 0.5, "avg_daily_freq": 1.0, "freq_drop": 0.1},
        "90": {"horizon": 90, "churn_rate": 0.2, "avg_daily_freq": 0.5, "freq_drop": 0.6}}})
    assert [r["horizon"] for r in rows] == [1, 90]
    md = notify.render_horizon_md(rows, 0.4)
    assert "**1 天**" in md and "⚠️" in md               # 1 天越线
    assert "加速出逃" in md                               # 短窗 ≥ 长窗 → 加速
    text = notify.render_horizon_text(rows, 0.4)
    assert all("**" not in line for line in text)


def test_render_run_status_surfaces_error():
    md = notify.render_run_status_md(_fake_status(ok=False, error="ValueError: boom"))
    assert "❌ 失败" in md and "报错内容" in md and "ValueError: boom" in md
    assert "① 数据抓取" in md
    html_text = notify.render_run_status_html(_fake_status(ok=False, error="ValueError: boom"))
    assert "ValueError: boom" in html_text and "<b>报错内容</b>" in html_text
    assert "✅ 成功" in notify.render_run_status_md(_fake_status())


def test_digest_text_leads_with_status_risk_and_windows():
    digest = {"rows": [], "n_addresses": 0, "n_personas": 0, "n_noise": 0, "risk_ratio": 0.0}
    summary = {"forecast_churn_rate": 0.62, "high_risk_ratio": 0.40, "avg_freq_drop": 9.0,
               "current_mean_freq": 20.0, "final_day_freq": 11.0, "horizon": 30,
               "horizon_forecasts": {
                   "7": {"horizon": 7, "churn_rate": 0.60, "avg_daily_freq": 12.0,
                         "freq_drop": 8.0}}}
    text = notify.build_digest_text(summary, 0.0, digest=digest, insights="· 结论",
                                    status=_fake_status())
    lines = text.splitlines()
    assert "每日简报" in lines[0]
    assert "运行状态" in lines[3]                 # 头部 3 行之后紧接着运行状态
    assert "🔴 高危" in text and "必须立刻重视" in text
    assert "多时间窗流失率预测" in text and "- 7 天：流失率 60.0%" in text
    assert "初步推进建议" in text
    assert "**" not in text and "<font" not in text   # 纯文本通道不带 markdown/HTML 标记
    assert "http://localhost:8501" in text


def test_digest_card_is_structured_markdown():
    digest = {"rows": [{"label": "高频大户", "n": 5, "pct": 0.5, "churn_rate": 0.6,
                        "freq": 4.0, "holding": 8.0, "protocols": 3.0, "gas_ratio": 0.2,
                        "balance": 1.0, "idle_days": 10.0}],
              "n_addresses": 10, "n_personas": 1, "n_noise": 0, "risk_ratio": 0.1}
    summary = {"forecast_churn_rate": 0.70, "high_risk_ratio": 0.50, "avg_freq_drop": 2.0,
               "horizon": 30,
               "horizon_forecasts": {"30": {"horizon": 30, "churn_rate": 0.70,
                                            "avg_daily_freq": 1.0, "freq_drop": 2.0}}}
    card = notify.build_digest_card(summary, 0.1, digest=digest, insights="· 结论",
                                    status=_fake_status())
    assert card["header"]["template"] == "red"          # 高危 → 红色标题栏
    assert card["header"]["title"]["content"].endswith("每日简报")
    content = " ".join(e["text"]["content"] for e in card["elements"] if e.get("text"))
    assert "**" in content                              # lark_md 真加粗
    assert "**二、用户行为聚类结果**" in content
    assert "初步推进建议" in content and "必须重视" in content
    assert any(e["tag"] == "hr" for e in card["elements"])


def test_suggestions_name_actions_and_stakeholders():
    digest = {"rows": [
        {"label": "高频套利者", "n": 8, "pct": 0.4, "churn_rate": 0.5, "freq": 1.0,
         "holding": 4.0, "protocols": 2.0, "gas_ratio": 0.9, "balance": -3.0, "idle_days": 60.0},
        {"label": "长期持有者", "n": 6, "pct": 0.3, "churn_rate": 0.9, "freq": 0.5,
         "holding": 30.0, "protocols": 5.0, "gas_ratio": 0.1, "balance": 2.0, "idle_days": 5.0}],
        "n_addresses": 20, "n_personas": 2, "n_noise": 0, "risk_ratio": 0.4}
    tips = notify.build_suggestions({"horizon": 30}, digest, _fake_status(ok=False),
                                    notify.risk_level(0.70))
    joined = " ".join(tips)
    assert "先修数据管道" in joined                    # 运行失败 → 先修管道
    assert "主攻「高频套利者」" in joined and "增长 / 运营" in joined
    assert "守住基本盘" in joined and "长期持有者" in joined
    assert "Etherscan" in joined and "数据/平台工程" in joined


def test_status_page_writes_html(tmp_path, monkeypatch):
    from src import status_page
    monkeypatch.setattr(status_page.config, "STATUS_PAGE", tmp_path / "status.html")
    path = status_page.build_status_page(
        {"forecast_churn_rate": 0.62, "high_risk_ratio": 0.4, "horizon": 30},
        digest=notify.collect_cluster_digest(tmp_path / "nope.db"),
        status=_fake_status(), email_ok=True, lark_ok=True,
        n_addresses=2000, n_transactions=12000)
    text = path.read_text(encoding="utf-8")
    assert "运行状态" in text and "三个方向的入口与结果" in text
    assert "初步推进建议" in text and "http://localhost:8501" in text
    assert "2000 / 12000" in text


def test_parse_oauth_redirect_accepts_url_and_raw_code():
    url = ("http://localhost:8765/?iss=https://accounts.google.com"
           "&code=4/0AXlqoi7d4NLeA&scope=https://www.googleapis.com/auth/gmail.send")
    parsed = notify.parse_oauth_redirect(url)
    assert parsed["code"] == "4/0AXlqoi7d4NLeA"          # 用户从地址栏整条 URL 贴回来
    assert parsed["iss"] == "https://accounts.google.com"
    assert notify.parse_oauth_redirect("4/0AXlqoi7d4NLeA")["code"] == "4/0AXlqoi7d4NLeA"
    assert notify.parse_oauth_redirect("code=xyz")["code"] == "xyz"
    err = notify.parse_oauth_redirect("http://localhost:8765/?error=access_denied")
    assert err["error"] == "access_denied"
    assert notify.parse_oauth_redirect("") == {}


@pytest.mark.parametrize("host,port", [("127.0.0.1", 18766), ("[::1]", 18767)])
def test_oauth_callback_server_listens_on_ipv4_and_ipv6(host, port):
    """回调用例：macOS 上 Chrome 把 localhost 解析成 ::1 —— 必须双栈监听。

    顺带回归「/favicon.ico 等杂包吃掉唯一一次 handle_request()」的老 bug。
    """
    import threading
    import time
    import urllib.request

    holder: dict = {}
    thread = threading.Thread(
        target=lambda: holder.update(
            notify._serve_callback(f"http://localhost:{port}/", 20)), daemon=True)
    thread.start()
    time.sleep(0.6)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico", timeout=5) as resp:
            assert resp.status == 204                     # 杂包 → 204，且不当作回调
    except OSError as exc:                                # pragma: no cover - 无 IPv6 的环境
        pytest.skip(f"该环境不支持本地回环监听: {exc}")
    assert holder == {}
    with urllib.request.urlopen(f"http://{host}:{port}/?code=ABC123", timeout=5) as resp:
        assert resp.status == 200
    thread.join(timeout=10)
    assert holder.get("code") == "ABC123"


def test_oauth_persist_env_value_updates_in_place(tmp_path, monkeypatch):
    env = tmp_path / "environment.env"
    env.write_text("SMTP_USER=a@b.c\nGMAIL_REFRESH_TOKEN=\n", encoding="utf-8")
    monkeypatch.setattr(notify.config, "PROJECT_ROOT", tmp_path)
    assert notify._persist_env_value("GMAIL_REFRESH_TOKEN", "NEW_TOKEN") == str(env)
    text = env.read_text(encoding="utf-8")
    assert "GMAIL_REFRESH_TOKEN=NEW_TOKEN" in text
    assert "SMTP_USER=a@b.c" in text                      # 其它行原样保留
    assert text.count("GMAIL_REFRESH_TOKEN=") == 1        # 原地替换，不重复追加
    notify._persist_env_value("FETCH_PROXY", "http://127.0.0.1:7897")
    assert "FETCH_PROXY=http://127.0.0.1:7897" in env.read_text(encoding="utf-8")


def test_fetch_proxy_config_and_client_routes(monkeypatch):
    from src import config, data_fetcher
    monkeypatch.setattr(config, "FETCH_PROXY", "127.0.0.1:7897")     # 省略 scheme 也能用
    assert config.http_proxies() == {"http": "http://127.0.0.1:7897",
                                     "https": "http://127.0.0.1:7897"}
    client = data_fetcher.EtherscanClient("KEY", delay=0)
    labels = [label for _, label in client.routes]
    assert labels[0].startswith("代理") and labels[-1] == "直连"      # 代理失败自动回落
    assert client.session.trust_env is True

    monkeypatch.setattr(config, "FETCH_PROXY", "direct")             # 强制直连
    assert config.http_proxies() is None
    direct = data_fetcher.EtherscanClient("KEY", delay=0)
    assert direct.proxies is None and direct.routes == [(None, "直连")]
    assert direct.session.trust_env is False                        # 不再读系统代理


def test_fetcher_falls_back_to_direct_when_proxy_is_down(monkeypatch):
    """代理挂了不能整个抓取都失败：整条代理重试链失败后自动切直连。"""
    from src import config, data_fetcher
    monkeypatch.setattr(config, "FETCH_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setattr(config, "HTTP_RETRIES", 2)
    monkeypatch.setattr(data_fetcher.time, "sleep", lambda *_: None)

    client = data_fetcher.EtherscanClient("KEY", delay=0)
    seen = []

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "1", "result": []}

    def fake_get(url, params=None, timeout=None, proxies=None):
        seen.append(proxies)
        if proxies:                                   # 代理不通
            raise data_fetcher.requests.ConnectTimeout("proxy down")
        return FakeResp()

    monkeypatch.setattr(client.session, "get", fake_get)
    assert client._get({"module": "account", "action": "txlist"}) == {"status": "1", "result": []}
    assert seen[0] is not None and seen[-1] is None    # 先走代理，再回落直连
    assert client.via == "直连"


def test_open_browser_prefers_launch_services(monkeypatch):
    """macOS 先试 ``/usr/bin/open``（LaunchServices，不依赖「自动化」授权）。"""
    calls = []

    class Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(notify.subprocess, "run",
                        lambda cmd, **kwargs: (calls.append(cmd), Proc())[1])
    assert notify.open_browser("https://example.com/x") is True
    assert calls == [["open", "https://example.com/x"]]      # 第一条就成功，不再往下试


def test_open_browser_falls_back_to_webbrowser_then_reports_failure(monkeypatch):
    """``open`` 全失败 → 退到 Python webbrowser；再失败 → 返回 False（不抛异常）。"""
    tried = []

    class Proc:
        returncode = 1
        stdout = ""
        stderr = "Unable to find application"

    monkeypatch.setattr(notify.subprocess, "run",
                        lambda cmd, **kwargs: (tried.append(cmd), Proc())[1])
    monkeypatch.setattr("webbrowser.open", lambda url: True)
    assert notify.open_browser("https://example.com/y") is True
    assert tried and tried[0][0] == "open"                    # 先试过 LaunchServices

    monkeypatch.setattr("webbrowser.open", lambda url: False)
    assert notify.open_browser("https://example.com/z") is False   # 全失败：False，不抛
    assert notify.open_browser("") is False                        # 空串安全返回


def test_open_browser_accepts_local_paths(monkeypatch):
    """传本地路径时自动转成 ``file://``（运行状态页走的就是这条路）。"""
    seen = []

    class Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(notify.subprocess, "run",
                        lambda cmd, **kwargs: (seen.append(cmd), Proc())[1])
    notify.open_browser("/tmp/status.html")
    assert seen[0][1].startswith("file:///") and seen[0][1].endswith("status.html")


def test_gmail_api_hint_turns_403_into_actionable_steps():
    hint = notify._gmail_api_hint(
        403, 'Gmail API has not been used in project 849097115397 before or it is disabled.')
    assert "gmail.googleapis.com" in hint and "849097115397" in hint and "启用" in hint
    assert "oauth-login" in notify._gmail_api_hint(401, '{"error": "invalid_grant"}')
    assert notify._gmail_api_hint(400, "some other error") == ""


def test_gmail_send_raises_typed_error_with_hint(monkeypatch):
    class Resp:
        status_code = 403
        text = 'Gmail API has not been used in project 849097115397 before'

    monkeypatch.setattr(notify, "_gmail_access_token", lambda: "token")
    monkeypatch.setattr(notify, "_post_json", lambda *a, **k: (Resp(), "代理"))
    with pytest.raises(notify.GmailApiError) as err:
        notify.send_email_via_gmail_api(notify.MIMEMultipart("alternative"))
    assert err.value.status == 403 and "849097115397" in err.value.hint


def test_send_email_falls_back_to_smtp_when_gmail_api_is_disabled(monkeypatch):
    """Gmail API 403（API 未启用）不是终点：自动改走 SMTP，邮件照样发得出去。"""
    from src import config
    monkeypatch.setattr(config, "GMAIL_CLIENT_ID", "cid")
    monkeypatch.setattr(config, "GMAIL_CLIENT_SECRET", "sec")
    monkeypatch.setattr(config, "GMAIL_REFRESH_TOKEN", "rt")
    monkeypatch.setattr(config, "SMTP_USER", "u@gmail.com")
    monkeypatch.setattr(config, "SMTP_PASS", "pw")
    monkeypatch.setattr(notify, "send_email_via_gmail_api",
                        lambda msg: (_ for _ in ()).throw(
                            notify.GmailApiError("HTTP 403", hint="hint", status=403)))
    used = {}
    monkeypatch.setattr(notify, "_send_email_via_smtp",
                        lambda msg, to: (used.update(to=to), True)[1])
    assert notify.send_email("主题", "<p>正文</p>", to="a@b.c") is True
    assert used["to"] == "a@b.c"


def test_send_email_without_smtp_credentials_reports_and_gives_up(monkeypatch):
    from src import config
    monkeypatch.setattr(config, "GMAIL_CLIENT_ID", "cid")
    monkeypatch.setattr(config, "GMAIL_CLIENT_SECRET", "sec")
    monkeypatch.setattr(config, "GMAIL_REFRESH_TOKEN", "rt")
    monkeypatch.setattr(config, "SMTP_USER", "")
    monkeypatch.setattr(config, "SMTP_PASS", "")
    monkeypatch.setattr(notify, "send_email_via_gmail_api",
                        lambda msg: (_ for _ in ()).throw(
                            notify.GmailApiError("HTTP 403", hint="hint", status=403)))
    assert notify.send_email("主题", "<p>正文</p>", to="a@b.c") is False


def test_enable_gmail_api_calls_serviceusage_and_polls(monkeypatch):
    """403 自愈：把 ``gmail.googleapis.com`` 在项目里启用，然后轮询到生效。"""
    monkeypatch.setattr(notify, "_gmail_access_token", lambda: "tok")
    monkeypatch.setattr(notify, "_gmail_project_number", lambda token: "849097115397")
    monkeypatch.setattr(notify, "_gmail_api_ready", lambda token: True)
    monkeypatch.setattr(notify.time, "sleep", lambda *_: None)
    posted = {}

    class Resp:
        status_code = 200
        text = "{}"

    def fake_post(url, payload, timeout=15, prefer_proxy=True, headers=None):
        posted["url"] = url
        return Resp(), "代理"

    monkeypatch.setattr(notify, "_post_json", fake_post)
    assert notify.enable_gmail_api() is True
    assert "projects/849097115397/services/gmail.googleapis.com:enable" in posted["url"]


def test_gmail_project_number_parsed_from_403(monkeypatch):
    class Resp:
        status_code = 403
        text = '{"error": {"message": "Gmail API has not been used in project 849097115397"}}'

    monkeypatch.setattr(notify, "_post_json", lambda *a, **k: (Resp(), "代理"))
    assert notify._gmail_project_number("tok") == "849097115397"

    class Ok(Resp):
        status_code = 200

    monkeypatch.setattr(notify, "_post_json", lambda *a, **k: (Ok(), "代理"))
    assert notify._gmail_project_number("tok") == ""          # 已启用 → 空串


def test_progress_bar_formats_percentage_and_width():
    from src import progress
    line = progress.bar(1245, 2000, label="抓取地址")
    assert "62%" in line and "1245/2000" in line and "抓取地址" in line
    assert line.count("█") + line.count("░") == progress.BAR_WIDTH
    assert progress.bar(0, 0).endswith("0/1")                 # 除零保护
    assert progress.bar(9, 3).endswith("3/3")                 # 越界截断


def test_smtp_proxy_reuses_fetch_proxy_unless_direct(monkeypatch):
    """只配一条出口线时，邮件/Google API 复用抓取出口；抓取配 direct 则都不走代理。"""
    import importlib
    from src import config
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)   # 屏蔽真实 env 文件
    for name in ("SMTP_PROXY", "NET_PROXY", "FETCH_PROXY"):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("FETCH_PROXY", "127.0.0.1:7897")
    reloaded = importlib.reload(config)
    assert reloaded.SMTP_PROXY == "http://127.0.0.1:7897"
    assert reloaded.NET_PROXY == "http://127.0.0.1:7897"

    monkeypatch.setenv("FETCH_PROXY", "direct")
    reloaded = importlib.reload(config)
    assert reloaded.SMTP_PROXY == "" and reloaded.NET_PROXY == ""

    monkeypatch.undo()                       # 还原环境变量与 dotenv 打桩
    importlib.reload(config)                 # 重新读回 environment.env，别影响后续用例


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
