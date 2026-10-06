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
FORECAST_HORIZON_DAYS = _get_int("FORECAST_HORIZON_DAYS", 30)

# --- Alerts -------------------------------------------------------------------
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _get_int("SMTP_PORT", 465)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "dingbangchu@gmail.com")
LARK_WEBHOOK_URL = os.getenv("LARK_WEBHOOK_URL", "")
LARK_AT_PHONE = os.getenv("LARK_AT_PHONE", "13339947334")
ALERT_CHURN_RATE_THRESHOLD = _get_float("ALERT_CHURN_RATE_THRESHOLD", 0.40)
ALERT_RISK_CLUSTER_RATIO = _get_float("ALERT_RISK_CLUSTER_RATIO", 0.25)

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:8501")
