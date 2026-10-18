"""Render用Webサーバーと取引ループ。"""

import logging
import os
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify

from config import FETCH_INTERVAL_SEC, SYMBOL, TRADING_MODE
from improved.data_fetcher import fetch_klines
from improved.strategy import generate_signal
from improved.notifier import notify
from improved.leader_lock import try_acquire_or_renew

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

try:
    if TRADING_MODE == "demo":
        from improved.demo_trader import DemoTrader
        trader = DemoTrader()
        logger.info("[DEMO] Binance Testnetに接続して取引します")
    else:
        from improved.paper_trader import PaperTrader
        trader = PaperTrader()
except Exception:
    # ここで失敗するとgunicornのワーカーがそもそも起動できず、/pingすら応答しなくなる。
    # 原因調査の手がかりをTelegramにも残してから、通常通り例外を再送出してプロセスを終了させる
    # (中途半端な状態で起動を続けるより、明確に落として気づけるようにする)。
    logger.exception("トレーダーの初期化に失敗しました。起動を中止します。")
    notify(
        f"🔥 起動に失敗しました。トレーダー({TRADING_MODE})の初期化でエラーが発生しています。ログを確認してください。",
        level="error", blocking=True,
    )
    raise

latest_status = {"updated_at": None, "price": None, "signal": None, "action": None, "portfolio_value": None}
status_lock = threading.Lock()


def _trading_cycle(had_error_flag):
    """1回分の価格取得〜売買判定〜実行。成功/失敗に応じて通知の要否を判断する。"""
    global latest_status

    # 複数プロセス(gunicornワーカーなど)が同時に動いていても、取引を実行するのは
    # リーダー権を持つ1プロセスだけにする(二重発注・状態競合の防止)。
    # Redis未使用時は常にTrueが返るので、通常運用では何も変わらない。
    if not try_acquire_or_renew(ttl_sec=FETCH_INTERVAL_SEC * 3):
        logger.info("他のプロセスが取引ループのリーダーのため、このサイクルは見送ります")
        return

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
    return jsonify({
        "service": "btc-paper-trading-bot", "symbol": SYMBOL, "status": "running", "trading_mode": TRADING_MODE,
    })


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


def _should_start_trading_loop() -> bool:
    """
    本番運用(gunicorn)やdebug=Falseでのローカル実行(今の`python app.py`はこちら)では、
    このモジュールは1回しか読み込まれないため、常にスレッドを起動してよい
    (`app.debug`がFalseの場合)。

    一方、Flask/Werkzeugの自動リローダー(`app.run(debug=True)`を使った場合)では、
    親プロセスと子プロセスの両方でこのモジュールが読み込まれ、取引ループの
    スレッドが二重に起動してしまう。実際にリクエストを処理する子プロセス側
    (WERKZEUG_RUN_MAIN="true")だけで起動するよう、`app.debug`が真の場合に限って
    この環境変数を確認する(`app.debug`がFalseなら、WERKZEUG_RUN_MAINの値に
    関わらず単一プロセスでの実行とみなし、常に起動する)。

    なお、二重に起動してしまった場合でも実際の売買はleader_lock(Redis使用時)で
    1プロセスに制限されるため、これは「無駄なスレッド・ログの重複」を防ぐための
    追加の防御(Redis未使用時はこちらが唯一の防御線になる)。
    """
    if not app.debug:
        return True
    return os.environ.get("WERKZEUG_RUN_MAIN") == "true"


if _should_start_trading_loop():
    threading.Thread(target=run_trading_loop_with_watchdog, daemon=True).start()
else:
    logger.info("Werkzeugリローダーの親プロセスのため、取引ループスレッドの起動をスキップします")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
