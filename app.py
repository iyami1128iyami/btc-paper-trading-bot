"""Render用Webサーバーと取引ループ。"""

import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify

from config import FETCH_INTERVAL_SEC
from improved.data_fetcher import fetch_klines
from improved.strategy import generate_signal
from improved.paper_trader import PaperTrader

app = Flask(__name__)
trader = PaperTrader()
latest_status = {"updated_at": None, "price": None, "signal": None, "action": None, "portfolio_value": None}
status_lock = threading.Lock()


def trading_loop():
    global latest_status
    while True:
        try:
            prices = fetch_klines(limit=100)
            current_price = prices[-1]
            signal = generate_signal(prices)
            result = trader.execute(signal, current_price)
            value = trader.portfolio_value(current_price)
            with status_lock:
                latest_status = {"updated_at": datetime.now(timezone.utc).isoformat(), "price": current_price, "signal": signal, "action": result, "portfolio_value": round(value, 2)}
            print(latest_status)
        except Exception as exc:
            print(f"エラーが発生しました: {exc}")
        time.sleep(FETCH_INTERVAL_SEC)


@app.route("/")
def index():
    return jsonify({"service": "btc-paper-trading-bot", "status": "running"})


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


threading.Thread(target=trading_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
