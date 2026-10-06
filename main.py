"""メインループ(ローカル実行用)。"""

import logging
import time
from datetime import datetime

from config import FETCH_INTERVAL_SEC
from improved.data_fetcher import fetch_klines
from improved.strategy import generate_signal
from improved.paper_trader import PaperTrader
from improved.notifier import notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def run_loop():
    trader = PaperTrader()
    print("ペーパートレードを開始します。Ctrl+Cで停止できます。")
    had_error = False
    while True:
        try:
            prices = fetch_klines(limit=100)
            current_price = prices[-1]
            signal = generate_signal(prices)
            result = trader.execute(signal, current_price)
            value = trader.portfolio_value(current_price)
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 価格: {current_price:.2f} | シグナル: {signal} | {result} | 評価資産: {value:.2f} USDT")
            if had_error:
                notify("✅ エラーから復旧しました。", level="recovery")
                had_error = False
        except Exception as exc:
            logger.exception("エラーが発生しました")
            print(f"エラーが発生しました: {exc}")
            if not had_error:
                notify(f"🚨 エラーが発生しました: {exc}", level="error")
                had_error = True
        time.sleep(FETCH_INTERVAL_SEC)


if __name__ == "__main__":
    run_loop()
