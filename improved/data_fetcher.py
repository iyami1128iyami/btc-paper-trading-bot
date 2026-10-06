"""
改善版：価格データ取得モジュール(CoinGecko版)

CoinGeckoの公開API(APIキー不要)から価格データを取得する。
米国などの一部リージョンからBinance APIが451エラーでブロックされる問題を
回避するため、地域制限のないCoinGeckoに切り替えている。

CoinGeckoの無料・キー無しプランはレート制限が緩くない(目安: 5〜30回/分)ため、
FETCH_INTERVAL_SEC(デフォルト60秒=1分に1回)程度のアクセス頻度を想定している。
もっと高頻度にしたい場合は、CoinGeckoの無料Demo APIキーを取得して
COINGECKO_API_KEY環境変数に設定すると、レート制限が緩和される(100回/分)。
"""

import logging
import math
import os
import time

import requests
from requests.exceptions import ConnectionError, RequestException, Timeout

from config import SYMBOL

logger = logging.getLogger(__name__)

BASE_URL = "https://api.coingecko.com/api/v3"
MAX_RETRY_AFTER_SEC = 300

# SYMBOL (例: "BTCUSDT") から CoinGecko の (coin_id, vs_currency) への対応表。
# 対応していないペアを使いたい場合は、COINGECKO_ID / COINGECKO_VS_CURRENCY
# 環境変数で直接指定すれば、この表を使わずに動作する。
SYMBOL_TO_COINGECKO = {
    "BTCUSDT": ("bitcoin", "usd"),
    "ETHUSDT": ("ethereum", "usd"),
    "SOLUSDT": ("solana", "usd"),
    "BNBUSDT": ("binancecoin", "usd"),
    "XRPUSDT": ("ripple", "usd"),
    "DOGEUSDT": ("dogecoin", "usd"),
    "ADAUSDT": ("cardano", "usd"),
    "BTCJPY": ("bitcoin", "jpy"),
    "ETHJPY": ("ethereum", "jpy"),
}


def _resolve_coin():
    """現在のSYMBOLに対応するCoinGeckoのcoin_idとvs_currencyを決定する。"""
    override_id = os.getenv("COINGECKO_ID")
    override_vs = os.getenv("COINGECKO_VS_CURRENCY")
    if override_id and override_vs:
        return override_id, override_vs

    if SYMBOL in SYMBOL_TO_COINGECKO:
        return SYMBOL_TO_COINGECKO[SYMBOL]

    raise ValueError(
        f"SYMBOL '{SYMBOL}' に対応するCoinGecko銘柄IDが見つかりません。"
        f"環境変数 COINGECKO_ID と COINGECKO_VS_CURRENCY を設定してください"
        f"(例: COINGECKO_ID=bitcoin, COINGECKO_VS_CURRENCY=usd)。"
    )


def _request_with_retry(url, params, retries, backoff_factor):
    """429(レート制限)と一時的なネットワークエラーに対してリトライする共通処理。"""
    api_key = os.getenv("COINGECKO_API_KEY")
    headers = {"x-cg-demo-api-key": api_key} if api_key else {}

    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=10)
            if resp.status_code == 429:
                try:
                    retry_after = int(resp.headers.get("Retry-After", "60") or "60")
                except (TypeError, ValueError):
                    # Retry-Afterが日付形式など数値以外で返ってくる場合に備えたフォールバック
                    retry_after = 60
                retry_after = max(1, min(retry_after, MAX_RETRY_AFTER_SEC))
                logger.warning("CoinGeckoのレート制限に達しました。%s秒待機します", retry_after)
                if attempt < retries - 1:
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()
            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError as exc:
                # resp.json()のデコード失敗(JSONDecodeErrorはValueErrorのサブクラス)もリトライ対象にする
                raise RequestException(f"CoinGeckoからのレスポンスがJSONとして解釈できません: {exc}") from exc
        except (ConnectionError, Timeout, RequestException) as exc:
            if attempt >= retries - 1:
                logger.error("CoinGecko APIの取得に失敗しました: %s", exc)
                raise
            wait_time = backoff_factor**attempt
            logger.warning("APIエラー。%.1f秒待機してリトライします: %s", wait_time, exc)
            time.sleep(wait_time)

    raise RuntimeError("CoinGecko API取得失敗")


def fetch_klines(limit: int = 100, retries: int = 3, backoff_factor: float = 2.0):
    """
    直近の価格データを取得する(古い順)。

    CoinGeckoの market_chart エンドポイントは「過去X日分」でしかリクエストできず、
    粒度(何分おきのデータか)はCoinGecko側が自動で決める(直近1日なら約5分間隔)。
    そのため、直近1日分を取得して末尾limit件を返す。
    """
    if limit <= 0:
        raise ValueError(f"limit must be positive: {limit}")
    if retries <= 0:
        raise ValueError(f"retries must be positive: {retries}")
    if backoff_factor < 0 or not math.isfinite(backoff_factor):
        raise ValueError(f"backoff_factor must be finite and non-negative: {backoff_factor}")

    coin_id, vs_currency = _resolve_coin()
    url = f"{BASE_URL}/coins/{coin_id}/market_chart"
    params = {"vs_currency": vs_currency, "days": 1}

    raw = _request_with_retry(url, params, retries, backoff_factor)

    prices_raw = raw.get("prices") if isinstance(raw, dict) else None
    if not isinstance(prices_raw, list) or not prices_raw:
        raise ValueError(f"無効なレスポンス形式: {raw}")

    closes = []
    for point in prices_raw:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            close = float(point[1])
        except (ValueError, TypeError):
            continue
        if not math.isfinite(close) or close <= 0:
            continue
        closes.append(close)

    if not closes:
        raise ValueError("有効な価格データが取得できません")

    return closes[-limit:] if len(closes) > limit else closes


def fetch_latest_price(retries: int = 3, backoff_factor: float = 2.0):
    """最新価格を1つだけ取得する(/simple/price、軽量)。"""
    coin_id, vs_currency = _resolve_coin()
    url = f"{BASE_URL}/simple/price"
    params = {"ids": coin_id, "vs_currencies": vs_currency}

    raw = _request_with_retry(url, params, retries, backoff_factor)

    try:
        price = float(raw[coin_id][vs_currency])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"無効な価格レスポンス: {raw}") from exc

    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"不正な価格を受信しました: {price}")

    return price


def fetch_historical_closes(total: int = 1000, retries: int = 3, backoff_factor: float = 2.0):
    """
    バックテスト用にまとまった量の過去価格を取得する(古い順)。

    CoinGeckoの market_chart は「過去何日分」でしかリクエストできず、
    データの粒度は期間に応じて自動で決まる(目安):
      - 1日以内    : 約5分間隔(最大 約288本)
      - 2〜90日    : 約1時間間隔
      - 91日以上   : 1日間隔
    必要な本数(total)から、それをカバーできそうな日数を逆算してリクエストする。
    """
    if total <= 0:
        raise ValueError(f"total must be positive: {total}")

    coin_id, vs_currency = _resolve_coin()
    url = f"{BASE_URL}/coins/{coin_id}/market_chart"

    if total <= 280:
        days = 1
    elif total <= 24 * 89:
        days = max(2, math.ceil(total / 24))
    else:
        days = total  # 1日1本として概算

    params = {"vs_currency": vs_currency, "days": days}
    raw = _request_with_retry(url, params, retries, backoff_factor)

    prices_raw = raw.get("prices") if isinstance(raw, dict) else None
    if not isinstance(prices_raw, list) or not prices_raw:
        raise ValueError(f"無効なレスポンス形式: {raw}")

    closes = []
    for point in prices_raw:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            close = float(point[1])
        except (ValueError, TypeError):
            continue
        if not math.isfinite(close) or close <= 0:
            continue
        closes.append(close)

    if not closes:
        raise ValueError("有効な価格データが取得できません")

    return closes[-total:] if len(closes) > total else closes
