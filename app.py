"""
Render にデプロイするためのWebサーバー。

Renderの無料/Starterプランでは常時起動するWebサービスとして
ヘルスチェック用のHTTPエンドポイントを持たせておくと運用しやすいため、
Flaskで簡単なステータス確認APIを立てつつ、
裏側のスレッドで売買ループを回す構成にしている。
"""

import threading
import time
from datetime import datetime

from flask import Flask, jsonify

from config import FETCH_INTERVAL_SEC
from data_fetcher import fetch_klines
from strategy import generate_signal
from paper_trader import PaperTrader

app = Flask(__name__)
trader = PaperTrader()

# 直近の状態をメモリ上にも保持しておき、/status で確認できるようにする
latest_status = {
    "updated_at": None,
    "price": None,
    "signal": None,
    "action": None,
    "portfolio_value": None,
}


def trading_loop():
    global latest_status
    while True:
        try:
            prices = fetch_klines(limit=100)
            current_price = prices[-1]
            signal = generate_signal(prices)
            result = trader.execute(signal, current_price)
            value = trader.portfolio_value(current_price)

            latest_status = {
                "updated_at": datetime.utcnow().isoformat(),
                "price": current_price,
                "signal": signal,
                "action": result,
                "portfolio_value": round(value, 2),
            }
            print(latest_status)

        except Exception as e:
            print(f"エラーが発生しました: {e}")

        time.sleep(FETCH_INTERVAL_SEC)


@app.route("/")
def index():
    return jsonify({"service": "btc-paper-trading-bot", "status": "running"})


@app.route("/status")
def status():
    return jsonify(latest_status)


@app.route("/ping")
def ping():
    """
    UptimeRobotなどの外部監視サービスからの定期アクセス用エンドポイント。
    価格取得や計算を行わず即座に返すだけなので、スリープ防止用のpingに向いている。
    """
    return "pong", 200


@app.route("/state")
def state():
    return jsonify(trader.state)


# アプリ起動時に売買ループをバックグラウンドスレッドで開始
threading.Thread(target=trading_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
