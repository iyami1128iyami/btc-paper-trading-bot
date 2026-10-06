"""
設定ファイル

ボット全体で使用される定数をここで一元管理。
環境変数の値が不正な場合は、起動時にわかりやすいエラーメッセージで止める
(例: 数値のつもりが空文字列になっている、範囲外の値になっている等)。
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _get_int(name: str, default: str) -> int:
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"環境変数 {name} は整数で指定してください(現在の値: {raw!r})") from exc


def _get_float(name: str, default: str) -> float:
    raw = os.getenv(name, default)
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"環境変数 {name} は数値で指定してください(現在の値: {raw!r})") from exc


# ========== 価格取得API設定(CoinGecko) ==========
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
# INTERVAL: CoinGecko移行に伴い現在未使用(以前のBinance APIでの名残)。
# CoinGeckoはローソク足間隔を指定できず、取得期間に応じて自動で粒度が決まる。
INTERVAL = os.getenv("INTERVAL", "5m")

# ========== 売買パラメータ ==========
FETCH_INTERVAL_SEC = _get_int("FETCH_INTERVAL_SEC", "60")  # API呼び出し間隔(秒)
INITIAL_BALANCE_USDT = _get_float("INITIAL_BALANCE_USDT", "1000.0")  # 初期資金(USDT)
TRADE_RATIO = _get_float("TRADE_RATIO", "0.95")  # 1回の売買で使用する残高の割合(0.0〜1.0)

if FETCH_INTERVAL_SEC <= 0:
    raise RuntimeError(f"FETCH_INTERVAL_SEC は正の整数にしてください(現在の値: {FETCH_INTERVAL_SEC})")
if INITIAL_BALANCE_USDT <= 0:
    raise RuntimeError(f"INITIAL_BALANCE_USDT は正の数値にしてください(現在の値: {INITIAL_BALANCE_USDT})")
if not 0 < TRADE_RATIO <= 1:
    raise RuntimeError(f"TRADE_RATIO は0より大きく1以下にしてください(現在の値: {TRADE_RATIO})")

# ========== 戦略パラメータ ==========
SHORT_WINDOW = _get_int("SHORT_WINDOW", "5")  # 短期移動平均の期間
LONG_WINDOW = _get_int("LONG_WINDOW", "20")  # 長期移動平均の期間
RSI_PERIOD = _get_int("RSI_PERIOD", "14")  # RSIの計算期間

if SHORT_WINDOW <= 0 or LONG_WINDOW <= 0:
    raise RuntimeError("SHORT_WINDOW / LONG_WINDOW は正の整数にしてください")
if SHORT_WINDOW >= LONG_WINDOW:
    raise RuntimeError(
        f"SHORT_WINDOW({SHORT_WINDOW})はLONG_WINDOW({LONG_WINDOW})より小さくしてください"
        f"(短期移動平均は長期移動平均より短い期間である必要があります)"
    )
if LONG_WINDOW < RSI_PERIOD:
    # 致命的ではないが、このままだとRSIが常にNoneになりBUY/SELLが一切発生しなくなるため、
    # 気づきにくい「無言の不具合」を防ぐために警告だけ出しておく。
    import logging

    logging.getLogger(__name__).warning(
        "LONG_WINDOW(%s)がRSI_PERIOD(%s)未満です。"
        "RSIが計算できる本数に達するまでシグナルが一切発生しません。",
        LONG_WINDOW, RSI_PERIOD,
    )

# ========== ファイルパス ==========
# ペアごとに状態・ログを分離する(切り替え時に別ペアの残高と混ざらないようにするため)
LOG_FILE = os.getenv("LOG_FILE", f"trade_log_{SYMBOL}.csv")
STATE_FILE = os.getenv("STATE_FILE", f"state_{SYMBOL}.json")
