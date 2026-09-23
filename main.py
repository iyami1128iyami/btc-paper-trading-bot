"""メインループ。"""

import time
from datetime import datetime

from config import FETCH_INTERVAL_SEC
from improved.data_fetcher import fetch_klines
from improved.strategy import generate_signal
from improved.paper_trader import PaperTrader


def run_loop():
    trader = PaperTrader()
    print("ペーパートレードを開始します。Ctrl+Cで停止できます。")
    while True:
        try:
            prices = fetch_klines(limit=100)
            current_price = prices[-1]
            signal = generate_signal(prices)
            result = trader.execute(signal, current_price)
            value = trader.portfolio_value(current_price)
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 価格: {current_price:.2f} | シグナル: {signal} | {result} | 評価資産: {value:.2f} USDT")
        except Exception as exc:
            print(f"エラーが発生しました: {exc}")
        time.sleep(FETCH_INTERVAL_SEC)


if __name__ == "__main__":
    run_loop()
