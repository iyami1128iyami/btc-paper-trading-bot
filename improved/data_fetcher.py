"""
改善版：価格データ取得モジュール

Binanceの公開API(認証不要)からローソク足データを取得する。
リトライ機構、レート制限対応、レスポンス検証を提供する。
"""

import logging
import math
import time

import requests
from requests.exceptions import ConnectionError, RequestException, Timeout

from config import INTERVAL, SYMBOL

logger = logging.getLogger(__name__)

BASE_URL = "https://api.binance.com/api/v3/klines"
MAX_RETRY_AFTER_SEC = 300


def _retry_after_seconds(response) -> int:
    try:
        value = int(response.headers.get("Retry-After", "60"))
    except (TypeError, ValueError):
        value = 60
    return max(0, min(value, MAX_RETRY_AFTER_SEC))


def fetch_klines(limit: int = 100, retries: int = 3, backoff_factor: float = 2.0):
    """直近のローソク足の終値を古い順に取得する。"""
    if limit <= 0:
        raise ValueError(f"limit must be positive: {limit}")
    if retries <= 0:
        raise ValueError(f"retries must be positive: {retries}")
    if backoff_factor < 0 or not math.isfinite(backoff_factor):
        raise ValueError(f"backoff_factor must be finite and non-negative: {backoff_factor}")

    params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": limit}

    for attempt in range(retries):
        try:
            logger.debug("API呼び出し試行: %s/%s", attempt + 1, retries)
            resp = requests.get(BASE_URL, params=params, timeout=10)

            if resp.status_code == 429:
                retry_after = _retry_after_seconds(resp)
                logger.warning("レート制限に達しました。%s秒待機します", retry_after)
                if attempt < retries - 1:
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()

            resp.raise_for_status()
            raw = resp.json()
            if not isinstance(raw, list) or not raw:
                raise ValueError(f"無効なレスポンス形式: {raw}")

            closes = []
            for candle in raw:
                if not isinstance(candle, (list, tuple)) or len(candle) < 5:
                    logger.warning("不完全なキャンドルデータをスキップ: %s", candle)
                    continue
                try:
                    close = float(candle[4])
                except (ValueError, TypeError):
                    logger.warning("終値変換失敗: %s", candle[4])
                    continue
                if not math.isfinite(close) or close <= 0:
                    logger.warning("不正な終値をスキップ: %s", candle[4])
                    continue
                closes.append(close)

            if not closes:
                raise ValueError("有効な終値データが取得できません")
            return closes

        except (ConnectionError, Timeout, RequestException) as exc:
            if attempt >= retries - 1:
                logger.error("API取得に失敗しました: %s", exc)
                raise
            wait_time = backoff_factor**attempt
            logger.warning("APIエラー。%.1f秒待機してリトライします: %s", wait_time, exc)
            time.sleep(wait_time)

    raise RuntimeError("API取得失敗")


def fetch_latest_price(retries: int = 3):
    closes = fetch_klines(limit=1, retries=retries)
    return closes[-1]


def fetch_historical_closes(total: int = 1000, retries: int = 3, backoff_factor: float = 2.0):
    """
    バックテスト用に、古い方向へページネーションしながら大量の終値を取得する。
    Binance APIは1リクエストあたり最大1000本までしか返さないため、
    `endTime` を過去にずらしながら複数回に分けて取得する。

    Returns:
        list[float]: 終値のリスト(古い順)
    """
    if total <= 0:
        raise ValueError(f"total must be positive: {total}")

    all_closes: list[float] = []
    end_time = None
    remaining = total

    while remaining > 0:
        batch_limit = min(remaining, 1000)
        params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": batch_limit}
        if end_time is not None:
            params["endTime"] = end_time

        raw = None
        for attempt in range(retries):
            try:
                resp = requests.get(BASE_URL, params=params, timeout=10)
                if resp.status_code == 429:
                    retry_after = _retry_after_seconds(resp)
                    logger.warning("レート制限に達しました。%s秒待機します", retry_after)
                    if attempt < retries - 1:
                        time.sleep(retry_after)
                        continue
                    resp.raise_for_status()
                resp.raise_for_status()
                raw = resp.json()
                break
            except (ConnectionError, Timeout, RequestException) as exc:
                if attempt >= retries - 1:
                    logger.error("過去データ取得に失敗しました: %s", exc)
                    raise
                time.sleep(backoff_factor**attempt)

        if not isinstance(raw, list) or not raw:
            break

        batch_closes = []
        oldest_open_time = None
        for candle in raw:
            if not isinstance(candle, (list, tuple)) or len(candle) < 5:
                continue
            try:
                close = float(candle[4])
                open_time = int(candle[0])
            except (ValueError, TypeError):
                continue
            if not math.isfinite(close) or close <= 0:
                continue
            batch_closes.append(close)
            if oldest_open_time is None:
                oldest_open_time = open_time

        if not batch_closes:
            break

        # 取得したバッチは新しい順ではなく古い順で返ってくるので、先頭に追加していく
        all_closes = batch_closes + all_closes
        remaining -= len(batch_closes)

        if oldest_open_time is None:
            break
        # 次のリクエストは、このバッチの最も古いローソク足より前を取得する
        end_time = oldest_open_time - 1

        if len(raw) < batch_limit:
            # これ以上過去のデータが存在しない
            break

    return all_closes[-total:] if len(all_closes) > total else all_closes
