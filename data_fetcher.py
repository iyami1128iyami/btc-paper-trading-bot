"""
価格データ取得モジュール

Binanceの公開API(認証不要)からローソク足データを取得する。
将来、実際に使う取引所(bitFlyer, GMOコインなど)が決まったら
このモジュールだけを差し替えれば、戦略・売買ロジックはそのまま使い回せる。
"""

import logging
import time

import requests
from requests.exceptions import ConnectionError, RequestException, Timeout

from config import SYMBOL, INTERVAL

logger = logging.getLogger(__name__)

BASE_URL = "https://api.binance.com/api/v3/klines"


def fetch_klines(limit: int = 100, retries: int = 3, backoff_factor: float = 2.0):
    """
    直近のローソク足データを取得する。

    API失敗時は指数バックオフでリトライ。
    レート制限エラーは自動的に待機して再試行。
    """
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "limit": limit,
    }

    for attempt in range(retries):
        try:
            logger.debug(f"API呼び出し試行: {attempt + 1}/{retries}")
            resp = requests.get(BASE_URL, params=params, timeout=10)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                logger.warning(f"レート制限に達しました。{retry_after}秒待機します")
                time.sleep(retry_after)
                continue

            resp.raise_for_status()
            raw = resp.json()

            if not isinstance(raw, list) or len(raw) == 0:
                raise ValueError(f"無効なレスポンス形式: {raw}")

            closes = []
            for candle in raw:
                if len(candle) < 5:
                    logger.warning(f"不完全なキャンドルデータをスキップ: {candle}")
                    continue
                try:
                    closes.append(float(candle[4]))
                except (ValueError, TypeError) as e:
                    logger.warning(f"終値変換失敗: {candle[4]} - {e}")
                    continue

            if not closes:
                raise ValueError("有効な終値データが取得できません")

            logger.info(f"API成功: {len(closes)}本のキャンドルを取得")
            return closes

        except (ConnectionError, Timeout) as e:
            logger.warning(f"接続エラー (試行 {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                wait_time = backoff_factor**attempt
                logger.info(f"{wait_time:.1f}秒待機してリトライします")
                time.sleep(wait_time)
            else:
                logger.error("最大リトライ回数に達しました")
                raise

        except RequestException as e:
            logger.error(f"API エラー: {e}")
            if attempt < retries - 1:
                wait_time = backoff_factor**attempt
                time.sleep(wait_time)
            else:
                raise

    raise RuntimeError("API取得失敗")


def fetch_latest_price(retries: int = 3):
    """最新の価格(現在値)を取得する"""
    try:
        closes = fetch_klines(limit=1, retries=retries)
        return closes[-1]
    except Exception as e:
        logger.error(f"最新価格取得失敗: {e}")
        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    try:
        prices = fetch_klines(limit=5)
        print(f"直近5本の終値: {prices}")
        print(f"最新価格: {prices[-1]:.2f}")
    except Exception as e:
        print(f"エラー: {e}")
