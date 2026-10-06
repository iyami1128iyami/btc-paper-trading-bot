"""Render用Webサーバーと取引ループ。"""

import logging
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify

from config import FETCH_INTERVAL_SEC, SYMBOL
from improved.data_fetcher import fetch_klines
from improved.strategy import generate_signal
from improved.paper_trader import PaperTrader
from improved.notifier import notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

try:
    trader = PaperTrader()
except Exception:
    # ここで失敗するとgunicornのワーカーがそもそも起動できず、/pingすら応答しなくなる。
    # 原因調査の手がかりをTelegramにも残してから、通常通り例外を再送出してプロセスを終了させる
    # (中途半端な状態で起動を続けるより、明確に落として気づけるようにする)。
    logger.exception("PaperTraderの初期化に失敗しました。起動を中止します。")
    notify("🔥 起動に失敗しました。PaperTraderの初期化でエラーが発生しています。ログを確認してください。", level="error")
    raise

latest_status = {"updated_at": None, "price": None, "signal": None, "action": None, "portfolio_value": None}
status_lock = threading.Lock()


def _trading_cycle(had_error_flag):
    """1回分の価格取得〜売買判定〜実行。成功/失敗に応じて通知の要否を判断する。"""
    global latest_status
    prices = fetch_klines(limit=100)
    current_price = prices[-1]
    signal = generate_signal(prices)
    result = trader.execute(signal, current_price)
    value = trader.portfolio_value(current_price)

    with status_lock:
        latest_status = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "price": current_price,
            "signal": signal,
            "action": result,
            "portfolio_value": round(value, 2),
        }
    logger.info("%s", latest_status)

    if had_error_flag["value"]:
        notify("✅ エラーから復旧し、価格取得・売買判定を再開しました。", level="recovery")
        had_error_flag["value"] = False


def trading_loop():
    had_error_flag = {"value": False}
    while True:
        try:
            _trading_cycle(had_error_flag)
        except Exception as exc:
            logger.exception("取引ループでエラーが発生しました")
            if not had_error_flag["value"]:
                # 同じエラーが続いている間は毎回通知せず、発生時と復旧時だけ知らせる(通知の洪水防止)。
                notify(f"🚨 取引ループでエラーが発生しました: {exc}\nリトライを継続します。", level="error")
                had_error_flag["value"] = True
        time.sleep(FETCH_INTERVAL_SEC)


def run_trading_loop_with_watchdog():
    """
    trading_loop()が(本来は起こり得ないはずだが)予期せず終了した場合に、
    検知して通知したうえで再起動する。これが無いと、バックグラウンドスレッドが
    静かに死んでWebサーバーだけが動き続け、売買が止まっていることに誰も気づけない。
    """
    while True:
        try:
            trading_loop()
            # trading_loop()は本来無限ループなので、ここに到達すること自体が異常
            logger.error("取引ループが予期せず終了しました。再起動します。")
            notify("🚨 取引ループが予期せず終了しました。自動的に再起動します。", level="error")
        except Exception:
            logger.exception("取引ループが例外で停止しました。再起動します。")
            notify("🚨 取引ループが致命的なエラーで停止しました。自動的に再起動します。", level="error")
        time.sleep(5)


@app.route("/")
def index():
    return jsonify({"service": "btc-paper-trading-bot", "symbol": SYMBOL, "status": "running"})


@app.route("/status")
def status():
    with status_lock:
        return jsonify(dict(latest_status))


@app.route("/ping")
def ping():
    return "pong", 200


@app.route("/state")
def state():
    return jsonify(trader.snapshot())


threading.Thread(target=run_trading_loop_with_watchdog, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
