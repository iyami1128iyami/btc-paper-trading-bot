"""
価格データ取得モジュール

Binanceの公開API(認証不要)からローソク足データを取得する。
将来、実際に使う取引所(bitFlyer, GMOコインなど)が決まったら
このモジュールだけを差し替えれば、戦略・売買ロジックはそのまま使い回せる。
"""

import requests
from config import SYMBOL, INTERVAL

BASE_URL = "https://api.binance.com/api/v3/klines"


def fetch_klines(limit: int = 100):
    """
    直近のローソク足データを取得する。

    Returns:
        list[float]: 終値(close price)のリスト(古い順)
    """
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "limit": limit,
    }
    resp = requests.get(BASE_URL, params=params, timeout=10)
    resp.raise_for_status()
    raw = resp.json()

    # Binanceのklines形式: [open_time, open, high, low, close, volume, ...]
    closes = [float(candle[4]) for candle in raw]
    return closes


def fetch_latest_price():
    """最新の価格(現在値)を1つだけ取得する"""
    closes = fetch_klines(limit=1)
    return closes[-1]


if __name__ == "__main__":
    # 動作確認用
    prices = fetch_klines(limit=5)
    print("直近5本の終値:", prices)
    print("最新価格:", fetch_latest_price())
