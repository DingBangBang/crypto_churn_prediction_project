"""Centralised configuration for the crypto churn project.

Reads environment variables from a local env file and exposes typed config values.

Env-file lookup order (first match wins):
  1. ``environment.env``  (the file the user creates for this project)
  2. ``environment .env`` (space-variant kept for parity with the Whale project)
  3. ``.env``
  4. Plain OS environment variables (this is what happens inside Docker).

The Etherscan API key is resolved from env first, then from the local
``Etherscan_api.txt`` scratch file (which is git-ignored).

Test mode
---------
When ``ADDRESS_LIMIT`` is small (<= 200) we consider the run a *test* run and
suffix every produced artefact (SQLite DB, figures, reports) with ``_test`` so the
throw-away test output never mixes with the real 2000-address dataset.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
REPORTS_DIR = PROJECT_ROOT / "reports"
DOCS_DIR = PROJECT_ROOT / "docs"

# --- env file loading ---------------------------------------------------------
_CANDIDATE_ENV_FILES = [
    PROJECT_ROOT / "environment.env",
    PROJECT_ROOT / "environment .env",
    PROJECT_ROOT / ".env",
]
for _env_file in _CANDIDATE_ENV_FILES:
    if _env_file.exists():
        load_dotenv(dotenv_path=str(_env_file), override=False)


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# --- Etherscan ----------------------------------------------------------------
ETHERSCAN_API_BASE_URL = os.getenv("ETHERSCAN_API_BASE_URL", "https://api.etherscan.io/v2/api")
CHAIN_ID = _get_int("CHAIN_ID", 1)


def _resolve_api_key() -> str:
    key = os.getenv("ETHERSCAN_API_KEY", "").strip()
    if key:
        return key
    scratch = PROJECT_ROOT / "Etherscan_api.txt"
    if scratch.exists():
        return scratch.read_text(encoding="utf-8", errors="ignore").strip()
    return ""


ETHERSCAN_API_KEY = _resolve_api_key()

# Free tier is ~5 req/s. Sleep between requests to be a good citizen.
BASE_RATE_LIMIT_DELAY = _get_float("BASE_RATE_LIMIT_DELAY", 0.22)
HTTP_RETRIES = _get_int("HTTP_RETRIES", 5)

# --- Dataset scope ------------------------------------------------------------
# Number of Ethereum addresses to sample & fetch. 200 -> test mode, 2000 -> full.
ADDRESS_LIMIT = _get_int("ADDRESS_LIMIT", 200)
MONTHS_BACK = _get_int("MONTHS_BACK", 6)
CHURN_DAYS = _get_int("CHURN_DAYS", 30)  # last_tx_days_ago > this == churned

# Bulk-pagination: how many records per Etherscan page (free tier allows up to 10000,
# so a single page usually covers a low/medium-activity address's whole 6-month history).
PAGE_SIZE = _get_int("PAGE_SIZE", 10000)
# Hard cap on pages per (address, endpoint) to bound the request budget.
MAX_PAGES = _get_int("MAX_PAGES", 2)

# A run is a "test" run when the address budget is small.
TEST_MODE = ADDRESS_LIMIT <= 200
RUN_TAG = "_test" if TEST_MODE else ""


def artefact(name: str) -> str:
    """Return ``name`` with the run tag inserted before the extension.

    ``crypto_churn.db`` -> ``crypto_churn_test.db`` in test mode.
    """
    if not RUN_TAG:
        return name
    stem, _, ext = name.partition(".")
    return f"{stem}{RUN_TAG}.{ext}" if ext else f"{name}{RUN_TAG}"


# --- Storage ------------------------------------------------------------------
DATA_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = os.getenv("CHURN_DB_PATH", str(DATA_DIR / artefact("crypto_churn.db")))

# --- Seed addresses -----------------------------------------------------------
# A small curated set of well-known, high-activity Ethereum addresses used as the
# "seed" population. The fetcher expands this list deterministically to
# ADDRESS_LIMIT addresses by walking the counterparties of the seed addresses.
_DEFAULT_SEEDS = ",".join(
    [
        "0xd8da6bf26964af9d7eed9e03e53415d37aa96045",  # vitalik.eth
        "0x28c6c06298d514db089934071355e5743bf21d60",  # Binance 14
        "0x21a31ee1afc51d94c2efccaa2092ad1028285549",  # Binance 15
        "0x71660c4005ba85c37ccec55d0c4493e66fe775d3",  # Coinbase 10
        "0x503828976d22510aad0201ac7ec88293211d23da",  # Coinbase 2
        "0x2910543af39aba0cd09dbb2d50200b3e800a63d2",  # Kraken 4
        "0x1f9090aae28b8a3dceadf281b0f12828e676c326",  # rsync-builder
        "0x742d35cc6634c0532925a3b844bc454e4438f44e",  # Bitfinex
        "0x47ac0fb4f2d84898e4d9e7b4dab3c24507a6d503",  # Binance 16
        "0xdfd5293d8e347dfe59e90efd55b2956a1343963d",  # Binance 17
    ]
)
SEED_ADDRESSES = [
    addr.strip()
    for addr in os.getenv("SEED_ADDRESSES", _DEFAULT_SEEDS).split(",")
    if addr.strip()
]

# --- Clustering ---------------------------------------------------------------
# Tuned so a 200-address sample yields compact, interpretable clusters instead of
# labelling most of the population as noise.
HDBSCAN_MIN_CLUSTER_SIZE = _get_int("HDBSCAN_MIN_CLUSTER_SIZE", 8)
HDBSCAN_MIN_SAMPLES = _get_int("HDBSCAN_MIN_SAMPLES", 2)

# --- Churn model --------------------------------------------------------------
TEST_SIZE = _get_float("TEST_SIZE", 0.3)
RANDOM_STATE = _get_int("RANDOM_STATE", 42)
# 主预测时间窗（30 天）：图表、模型阈值口径都以它为准。
FORECAST_HORIZON_DAYS = _get_int("FORECAST_HORIZON_DAYS", 30)

# --- 多时间窗预测 -------------------------------------------------------------
# 一次性给出 1 / 7 / 14 / 30 / 90 天几个时间窗的流失率（同一份 ARIMA 拟合到最大窗，
# 避免为每个窗口重复拟合）。主窗口 FORECAST_HORIZON_DAYS 会自动并入集合。
_RAW_HORIZONS = [
    h.strip() for h in (os.getenv("FORECAST_HORIZONS", "1,7,14,30,90") or "").split(",")
    if h.strip().isdigit()
]
FORECAST_HORIZONS = sorted({int(h) for h in _RAW_HORIZONS} | {FORECAST_HORIZON_DAYS}) or [30]
FORECAST_MAX_DAYS = max(FORECAST_HORIZONS)

# --- Alerts -------------------------------------------------------------------
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _get_int("SMTP_PORT", 465)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
# 本地/国内网络常对 SMTP 出站做阻断（TCP 可连、TLS 握手挂死）。
# 填上本地代理（如 Clash 混合端口 http://127.0.0.1:7897）即可让邮件走代理隧道发出。
SMTP_PROXY = os.getenv("SMTP_PROXY", "")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "dingbangchu@gmail.com")

# --- Lark (Feishu / Lark) 群机器人 ---------------------------------------------
# 只需一个 Incoming Webhook URL 即可推送；支持国际版 open.larksuite.com 与国内版 open.feishu.cn。
LARK_WEBHOOK_URL = os.getenv("LARK_WEBHOOK_URL", "")
# 可选 @ 某人：必须是 Lark 的 open_id (ou_...) / user_id，填手机号无法 @（代码会自动忽略）。
LARK_AT_ID = os.getenv("LARK_AT_ID", "") or os.getenv("LARK_AT_PHONE", "")
ALERT_CHURN_RATE_THRESHOLD = _get_float("ALERT_CHURN_RATE_THRESHOLD", 0.40)
ALERT_RISK_CLUSTER_RATIO = _get_float("ALERT_RISK_CLUSTER_RATIO", 0.25)

# --- 风险等级（简报里「是否高危 / 是否必须重视」的判定）------------------------
# 以「预测流失率 / ALERT_CHURN_RATE_THRESHOLD」的倍数分档：
#   >= 1.5x → 🔴 高危      （必须立刻重视，拉相关方开会）
#   >= 1.0x → 🟠 警戒      （已越线，进入处置流程）
#   >= 0.75x → 🟡 关注     （接近阈值，监控 + 提前准备召回）
#   否则     → 🟢 正常
RISK_WATCH_MULTIPLIER = _get_float("RISK_WATCH_MULTIPLIER", 0.75)
RISK_ALERT_MULTIPLIER = _get_float("RISK_ALERT_MULTIPLIER", 1.0)
RISK_CRITICAL_MULTIPLIER = _get_float("RISK_CRITICAL_MULTIPLIER", 1.5)

# --- 出站网络代理（邮件 / Google API 通用）--------------------------------------
# 国内网络对 Google 系域名（含 smtp.gmail.com、oauth2.googleapis.com）常做 TLS 层阻断，
# 只连 TCP 通、握手会挂死。统一走本地代理即可。
NET_PROXY = os.getenv("NET_PROXY", "") or SMTP_PROXY

# --- 出站网络代理（Etherscan 抓取专用）------------------------------------------
# 抓取走的是 https://api.etherscan.io。要不要走代理取决于线路：
#   * 本地开 Clash/mihomo 的 TUN/系统代理时，requests 会自动读系统代理（trust_env），
#     通常不填也能通；但**容器里没有系统代理**，必须显式给值。
#   * 优先级：FETCH_PROXY > 标准变量 HTTPS_PROXY/HTTP_PROXY > NET_PROXY（=SMTP_PROXY）。
#   * 特殊值 direct / none / off → 强制直连（绕过系统代理），便于排查与离线测试。
#   * Docker 里要用宿主机代理请填 http://host.docker.internal:7897（compose 已加 extra_hosts）。
FETCH_PROXY = (
    os.getenv("FETCH_PROXY", "").strip()
    or os.getenv("HTTPS_PROXY", "").strip()
    or os.getenv("https_proxy", "").strip()
    or os.getenv("HTTP_PROXY", "").strip()
    or os.getenv("http_proxy", "").strip()
    or NET_PROXY
)
# 「强制直连」的字面量（大小写不敏感）。
FETCH_PROXY_DIRECT_VALUES = ("direct", "none", "off")


def normalize_proxy(proxy: str) -> str:
    """``127.0.0.1:7897`` → ``http://127.0.0.1:7897``（已带 scheme 则原样返回）。"""
    proxy = (proxy or "").strip()
    if proxy and "//" not in proxy and not is_direct(proxy):
        proxy = "http://" + proxy
    return proxy


def is_direct(proxy: str) -> bool:
    """``direct`` / ``none`` / ``off`` → 强制直连（不读系统代理）。"""
    return (proxy or "").strip().lower() in FETCH_PROXY_DIRECT_VALUES


def http_proxies(proxy: str | None = None) -> dict | None:
    """``requests`` 用的 ``proxies`` 映射；空值/``direct`` 返回 ``None``（表示直连）。

    默认取 :data:`FETCH_PROXY`；传 ``proxy=""`` 可显式要求直连。
    """
    value = FETCH_PROXY if proxy is None else proxy
    if not value or is_direct(value):
        return None
    value = normalize_proxy(value)
    return {"http": value, "https": value}


# --- 邮件 / Google API 出口复用抓取出口 -----------------------------------------
# 只配了一条出口线（FETCH_PROXY）时，邮件与 Google API 也走同一条：
# 这样「断网只改一个变量」就能全局切换；抓取显式配了 direct/none/off 则邮件也不硬走代理。
if not SMTP_PROXY and not is_direct(FETCH_PROXY):
    SMTP_PROXY = normalize_proxy(FETCH_PROXY)
if not NET_PROXY:
    NET_PROXY = SMTP_PROXY

# --- Gmail OAuth2（Google 已下线「应用专用密码」后的正规发信方式）----------------
# 需要 Google Cloud OAuth 客户端（类型：桌面应用）：
#   GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET + 一次性授权拿到的 GMAIL_REFRESH_TOKEN
GMAIL_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET", "")
GMAIL_REFRESH_TOKEN = os.getenv("GMAIL_REFRESH_TOKEN", "")
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
# 可选：把「启用 Gmail API」也纳入授权范围，这样 403「API 未启用」能被代码自愈：
#   python -m src.notify --enable-gmail-api
# 只需多一次「允许」点击，之后代码自己调 Service Usage API 启用，不用去 Cloud Console 点。
GMAIL_ENABLE_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
SERVICEUSAGE_ENABLE_URL = ("https://serviceusage.googleapis.com/v1/projects/{project}"
                           "/services/gmail.googleapis.com:enable")
# 本地一次性授权用的回环地址（要与 OAuth 客户端里登记的重定向 URI 完全一致）
GMAIL_REDIRECT_URI = os.getenv("GMAIL_REDIRECT_URI", "http://localhost:8765/")

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:8501")
# 「运行状态页」：macOS 通知点开后展示运行状态 + 邮件/Lark/看板三方向链接与结果内容
STATUS_PAGE = REPORTS_DIR / "status.html"


# --- 口径一致性自检（看板 / 交付环节用）---------------------------------------
# 为什么需要：ADDRESS_LIMIT / TEST_MODE / DB_PATH 都是**导入时**求值的模块常量。长驻进程
# （Streamlit 看板）即使 ``st.cache_data(ttl=60)`` 过期，也只会用旧常量去读 `_test` 那套
# 200 地址的库 —— 表现为「看板永远不更新，还是测试结果」。这里提供运行期的交叉校验，把
# 「进程口径 vs 磁盘数据 vs 配置文件」的不一致显式暴露出来（看板渲染成顶部红条）。
def read_env_file_limit() -> int | None:
    """从 env 文件里直接读 ``ADDRESS_LIMIT``（不依赖启动时冻结的 ``os.environ``）。"""
    for path in _CANDIDATE_ENV_FILES:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("ADDRESS_LIMIT="):
                try:
                    return int(line.split("=", 1)[1].strip().strip('"').strip("'"))
                except ValueError:
                    return None
    return None


def feature_rows(db_name: str) -> int:
    """``data/<db_name>`` 里的特征地址数；库不存在或读不动返回 0。"""
    import sqlite3

    path = DATA_DIR / db_name
    if not path.exists():
        return 0
    try:
        conn = sqlite3.connect(path)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM address_features").fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return 0


def data_mode_drift_notice() -> str | None:
    """本进程口径与磁盘数据 / 配置文件不一致时给出提示文案，一致则 ``None``。"""
    full_rows = feature_rows("crypto_churn.db")
    test_rows = feature_rows("crypto_churn_test.db")
    file_limit = read_env_file_limit()
    restart = ("重启看板即可切换：容器 `docker compose restart churn-app`；"
               "宿主机 `pkill -f 'streamlit run app/dashboard.py'` 后用新口径重新运行。")
    if TEST_MODE and full_rows > 200:
        return (f"⚠️ **看板口径与数据不一致**：本进程按**测试口径**启动"
                f"（ADDRESS_LIMIT={ADDRESS_LIMIT}，正在读 `{Path(DB_PATH).name}` = {test_rows} 个地址），"
                f"但磁盘上已有**全量数据** `crypto_churn.db` = **{full_rows}** 个地址。"
                f"进程启动早于最后一次全量运行，模块常量不会热更新。{restart}")
    if file_limit is not None and file_limit != ADDRESS_LIMIT:
        return (f"⚠️ **env 文件改了但看板未重启**：文件里是 `ADDRESS_LIMIT={file_limit}`，"
                f"本进程启动时读到的是 `{ADDRESS_LIMIT}`。{restart}")
    if not TEST_MODE and full_rows == 0 and test_rows > 0:
        return (f"⚠️ 本进程按**全量口径**启动，但只找到测试库 `crypto_churn_test.db`"
                f"（{test_rows} 个地址），没有 `crypto_churn.db`。请先跑一次全量流水线"
                f"（`ADDRESS_LIMIT=2000 python scripts/run_pipeline.py`）。")
    return None
