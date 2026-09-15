"""
設定ファイル

ボット全体で使用される定数をここで一元管理。
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ========== Binance API 設定 ==========
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
INTERVAL = os.getenv("INTERVAL", "5m")

# ========== 売買パラメータ ==========
FETCH_INTERVAL_SEC = int(os.getenv("FETCH_INTERVAL_SEC", "60"))  # API呼び出し間隔(秒)
INITIAL_BALANCE_USDT = float(os.getenv("INITIAL_BALANCE_USDT", "1000.0"))  # 初期資金(USDT)
TRADE_RATIO = float(os.getenv("TRADE_RATIO", "0.95"))  # 1回の売買で使用する残高の割合(0.0～1.0)

# ========== 戦略パラメータ ==========
SHORT_WINDOW = int(os.getenv("SHORT_WINDOW", "5"))  # 短期移動平均の期間
LONG_WINDOW = int(os.getenv("LONG_WINDOW", "20"))  # 長期移動平均の期間

# ========== ファイルパス ==========
LOG_FILE = os.getenv("LOG_FILE", "trade_log.csv")
STATE_FILE = os.getenv("STATE_FILE", "state.json")
