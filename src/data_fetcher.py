"""Stage 1 — data acquisition.

Pulls normal / internal / ERC-20 transactions for a set of Ethereum addresses from
the **Etherscan V2** API and stores them in the SQLite ``raw_transactions`` table.

Highlights
----------
* Address expansion: starts from ``config.SEED_ADDRESSES`` and walks the observed
  counterparties (BFS) until ``ADDRESS_LIMIT`` unique addresses are collected, so a
  small seed list yields a 200/2000-address population deterministically.
* Pagination + rate limiting: pages of ``PAGE_SIZE`` (DESC by time), a sleep of
  ``BASE_RATE_LIMIT_DELAY`` between requests and exponential back-off on 429.
* Idempotent: writes use ``INSERT OR IGNORE`` on ``(address, tx_hash, tx_type)`` so
  re-running the fetcher only adds genuinely new rows.
* Test mode: with ``ADDRESS_LIMIT<=200`` the DB/figure names carry a ``_test`` suffix
  (see :mod:`src.config`).

Usage
-----
python src/data_fetcher.py               # uses ADDRESS_LIMIT from environment.env
python src/data_fetcher.py --limit 200   # override for a test run
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, db  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("churn.data_fetcher")

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
# Minimal ERC-20 symbol map for the tokens we most commonly see (best-effort).
_TOKEN_SYMBOLS = {
    "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
    "0x6b175474e89094c44da98b954eedeac495271d0f": "DAI",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH",
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "WBTC",
    "0x514910771af9ca656af840dff83e8264ecf986ca": "LINK",
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": "UNI",
    "0x7d1afa7b718fb893db30a3abc0cfc608aacfebb0": "MATIC",
    "0x4fabb145d64652a948d72526000c5a7e2be7f265": "BUSD",
}


class EtherscanClient:
    """Thin, rate-limited client around the Etherscan V2 API."""

    def __init__(self, api_key: str, base_url: str | None = None, delay: float | None = None):
        self.api_key = api_key
        self.base_url = base_url or config.ETHERSCAN_API_BASE_URL
        self.delay = config.BASE_RATE_LIMIT_DELAY if delay is None else delay
        self.retries = config.HTTP_RETRIES
        self.session = requests.Session()

    # -- low level -------------------------------------------------------------
    def _get(self, params: Dict[str, Any], timeout: int = 20) -> Optional[dict]:
        """GET one Etherscan call with retry/back-off; returns ``None`` on failure."""
        query = {**params, "chainid": str(config.CHAIN_ID), "apikey": self.api_key}
        for attempt in range(1, self.retries + 1):
            try:
                time.sleep(self.delay)
                resp = self.session.get(self.base_url, params=query, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                logger.warning("request failed (%s/%s) %s: %s", attempt, self.retries,
                               params.get("action"), exc)
                time.sleep(min(2 ** attempt, 10))
                continue

            result = data.get("result")
            if isinstance(result, str) and "rate limit" in result.lower():
                logger.warning("rate-limited on %s, backing off", params.get("action"))
                time.sleep(min(2 ** attempt, 15))
                continue
            return data
        return None

    def _paged(self, action: str, address: str, extra: Dict[str, Any] | None = None) -> List[dict]:
        """Fetch up to ``MAX_PAGES`` pages of a ``module=account`` endpoint."""
        rows: List[dict] = []
        for page in range(1, config.MAX_PAGES + 1):
            params = {
                "module": "account",
                "action": action,
                "address": address,
                "page": page,
                "offset": config.PAGE_SIZE,
                "sort": "desc",
            }
            if extra:
                params.update(extra)
            data = self._get(params)
            if not data:
                break
            result = data.get("result")
            if not isinstance(result, list) or not result:
                break  # "No transactions found" / empty page
            rows.extend(result)
            if len(result) < config.PAGE_SIZE:
                break
        return rows

    # -- public endpoints ------------------------------------------------------
    def normal_txs(self, address: str) -> List[dict]:
        return self._paged("txlist", address)

    def internal_txs(self, address: str) -> List[dict]:
        return self._paged("txlistinternal", address)

    def erc20_txs(self, address: str) -> List[dict]:
        return self._paged("tokentx", address)

    def eth_balance(self, address: str) -> float:
        data = self._get({"module": "account", "action": "balance", "address": address,
                          "tag": "latest"})
        try:
            return int(data["result"]) / 1e18  # type: ignore[index]
        except (TypeError, KeyError, ValueError):
            return 0.0


# --- normalisation helpers ----------------------------------------------------
def _wei_to_eth(value: Any) -> float:
    try:
        return int(value) / 1e18
    except (TypeError, ValueError):
        return 0.0


def _norm_normal(addr: str, tx: dict) -> Optional[Dict[str, Any]]:
    return {
        "address": addr,
        "tx_hash": tx.get("hash", ""),
        "block_number": int(tx.get("blockNumber") or 0),
        "timestamp": int(tx.get("timeStamp") or 0),
        "from_address": (tx.get("from") or "").lower(),
        "to_address": (tx.get("to") or "").lower(),
        "value_eth": _wei_to_eth(tx.get("value")),
        "value_usd": 0.0,
        "tx_type": "normal",
        "contract_address": "",
        "token_symbol": "ETH",
        "gas_used": int(tx.get("gasUsed") or 0),
        "gas_price": float(tx.get("gasPrice") or 0),
        "is_error": 1 if str(tx.get("isError")) == "1" else 0,
    }


def _norm_internal(addr: str, tx: dict) -> Optional[Dict[str, Any]]:
    if not tx.get("hash"):
        return None
    return {
        "address": addr,
        "tx_hash": tx.get("hash", "") + "_int",
        "block_number": int(tx.get("blockNumber") or 0),
        "timestamp": int(tx.get("timeStamp") or 0),
        "from_address": (tx.get("from") or "").lower(),
        "to_address": (tx.get("to") or "").lower(),
        "value_eth": _wei_to_eth(tx.get("value")),
        "value_usd": 0.0,
        "tx_type": "internal",
        "contract_address": "",
        "token_symbol": "ETH",
        "gas_used": int(tx.get("gas") or 0),
        "gas_price": 0.0,
        "is_error": 1 if str(tx.get("isError")) == "1" else 0,
    }


def _norm_erc20(addr: str, tx: dict) -> Optional[Dict[str, Any]]:
    contract = (tx.get("contractAddress") or "").lower()
    try:
        decimals = int(tx.get("tokenDecimal") or 18)
    except (TypeError, ValueError):
        decimals = 18
    try:
        raw = int(tx.get("value") or 0)
    except (TypeError, ValueError):
        raw = 0
    return {
        "address": addr,
        "tx_hash": tx.get("hash", ""),
        "block_number": int(tx.get("blockNumber") or 0),
        "timestamp": int(tx.get("timeStamp") or 0),
        "from_address": (tx.get("from") or "").lower(),
        "to_address": (tx.get("to") or "").lower(),
        "value_eth": raw / (10 ** decimals),  # token amount (not ETH)
        "value_usd": 0.0,
        "tx_type": "erc20",
        "contract_address": contract,
        "token_symbol": _TOKEN_SYMBOLS.get(contract, (tx.get("tokenSymbol") or "TOKEN")),
        "gas_used": int(tx.get("gasUsed") or 0),
        "gas_price": float(tx.get("gasPrice") or 0),
        "is_error": 0,
    }


def _counterparties(addr: str, rows: Iterable[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for r in rows:
        for side in ("from_address", "to_address"):
            other = r.get(side, "")
            if other and other != addr and other != ZERO_ADDRESS:
                out.append(other)
    return out


def _insert_rows(conn, rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    cur = conn.executemany(
        """
        INSERT OR IGNORE INTO raw_transactions
            (address, tx_hash, block_number, timestamp, from_address, to_address,
             value_eth, value_usd, tx_type, contract_address, token_symbol,
             gas_used, gas_price, is_error)
        VALUES (:address, :tx_hash, :block_number, :timestamp, :from_address, :to_address,
                :value_eth, :value_usd, :tx_type, :contract_address, :token_symbol,
                :gas_used, :gas_price, :is_error)
        """,
        rows,
    )
    conn.commit()
    return cur.rowcount


# --- orchestration ------------------------------------------------------------
def fetch_population(limit: int | None = None) -> List[str]:
    """BFS-expand the seed addresses to ``limit`` addresses and fetch their txs.

    Returns the list of watched addresses that were (attempted to be) fetched.
    """
    limit = limit or config.ADDRESS_LIMIT
    if not config.ETHERSCAN_API_KEY:
        raise RuntimeError(
            "ETHERSCAN_API_KEY is not set. Put it in environment.env or Etherscan_api.txt."
        )

    conn = db.connect()
    client = EtherscanClient(config.ETHERSCAN_API_KEY)

    queue: deque[str] = deque(a.lower() for a in config.SEED_ADDRESSES)
    seen: set[str] = set()
    watched: List[str] = []
    total_rows = 0
    t0 = time.time()

    logger.info("测试模式=%s | 目标地址数=%d | DB=%s", config.TEST_MODE, limit, config.DB_PATH)

    while len(watched) < limit and queue:
        addr = queue.popleft()
        if addr in seen:
            continue
        seen.add(addr)

        rows: List[Dict[str, Any]] = []
        for raw, norm in (
            (client.normal_txs(addr), _norm_normal),
            (client.internal_txs(addr), _norm_internal),
            (client.erc20_txs(addr), _norm_erc20),
        ):
            for tx in raw:
                rec = norm(addr, tx)
                if rec:
                    rows.append(rec)

        inserted = _insert_rows(conn, rows)
        total_rows += inserted
        watched.append(addr)

        for cp in _counterparties(addr, rows):
            if cp not in seen:
                queue.append(cp)

        if len(watched) % 10 == 0 or len(watched) == limit:
            elapsed = time.time() - t0
            logger.info(
                "已抓取 %d/%d 地址 | 新增 %d 条 | 队列 %d | 用时 %.0fs",
                len(watched), limit, total_rows, len(queue), elapsed,
            )

    db.record_run(conn, "data_fetcher", "ok",
                  f"addresses={len(watched)}, rows_inserted={total_rows}")
    conn.close()

    # Persist the population for reproducibility / dashboard.
    out = config.REPORTS_DIR / config.artefact("addresses.json")
    import json
    out.write_text(json.dumps(watched, indent=2), encoding="utf-8")
    logger.info("完成：%d 个地址，%d 条新记录，地址清单 -> %s", len(watched), total_rows, out)
    return watched


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch Ethereum transactions into SQLite.")
    parser.add_argument("--limit", type=int, default=None,
                        help="max addresses (defaults to ADDRESS_LIMIT)")
    args = parser.parse_args(argv)
    fetch_population(args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
