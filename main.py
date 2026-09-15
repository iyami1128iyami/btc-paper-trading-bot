"""
メインループ

定期的に価格を取得 → シグナル判定 → ペーパートレード実行 → ログ出力
を繰り返す。ローカルでの動作確認や、単体のワーカーとして動かす場合はこれを実行する。
"""

import time
from datetime import datetime

from config import FETCH_INTERVAL_SEC
from data_fetcher import fetch_klines
from strategy import generate_signal
from paper_trader import PaperTrader


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

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{now}] 価格: {current_price:.2f} | シグナル: {signal} | {result} | 評価資産: {value:.2f} USDT")

        except Exception as e:
            print(f"エラーが発生しました: {e}")

        time.sleep(FETCH_INTERVAL_SEC)


if __name__ == "__main__":
    run_loop()
