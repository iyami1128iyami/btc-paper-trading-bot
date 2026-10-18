"""メインループ(ローカル実行用)。"""

import logging
import time

from config import FETCH_INTERVAL_SEC, TRADING_MODE
from improved.data_fetcher import fetch_klines
from improved.strategy import generate_signal
from improved.notifier import notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def run_loop():
    if TRADING_MODE == "demo":
        from improved.demo_trader import DemoTrader
        trader = DemoTrader()
        logger.info("[DEMO] Binance Testnetに接続して取引します。Ctrl+Cで停止できます。")
    else:
        from improved.paper_trader import PaperTrader
        trader = PaperTrader()
        logger.info("ペーパートレードを開始します。Ctrl+Cで停止できます。")
    had_error = False
    while True:
        try:
            prices = fetch_klines(limit=100)
            current_price = prices[-1]
            signal = generate_signal(prices)
            result = trader.execute(signal, current_price)
            value = trader.portfolio_value(current_price)
            logger.info(
                "価格: %.2f | シグナル: %s | %s | 評価資産: %.2f USDT",
                current_price, signal, result, value,
            )
            if had_error:
                notify("✅ エラーから復旧しました。", level="recovery")
                had_error = False
        except Exception as exc:
            logger.exception("エラーが発生しました")
            if not had_error:
                notify(f"🚨 エラーが発生しました: {exc}", level="error")
                had_error = True
        time.sleep(FETCH_INTERVAL_SEC)


if __name__ == "__main__":
    run_loop()
