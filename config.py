"""
設定ファイル

ボット全体で使用される定数をここで一元管理。
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ========== 価格取得API設定(CoinGecko) ==========
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
# INTERVAL: CoinGecko移行に伴い現在未使用(以前のBinance APIでの名残)。
# CoinGeckoはローソク足間隔を指定できず、取得期間に応じて自動で粒度が決まる。
INTERVAL = os.getenv("INTERVAL", "5m")

# ========== 売買パラメータ ==========
FETCH_INTERVAL_SEC = int(os.getenv("FETCH_INTERVAL_SEC", "60"))  # API呼び出し間隔(秒)
INITIAL_BALANCE_USDT = float(os.getenv("INITIAL_BALANCE_USDT", "1000.0"))  # 初期資金(USDT)
TRADE_RATIO = float(os.getenv("TRADE_RATIO", "0.95"))  # 1回の売買で使用する残高の割合(0.0～1.0)

# ========== 戦略パラメータ ==========
SHORT_WINDOW = int(os.getenv("SHORT_WINDOW", "5"))  # 短期移動平均の期間
LONG_WINDOW = int(os.getenv("LONG_WINDOW", "20"))  # 長期移動平均の期間

# ========== ファイルパス ==========
# ペアごとに状態・ログを分離する(切り替え時に別ペアの残高と混ざらないようにするため)
LOG_FILE = os.getenv("LOG_FILE", f"trade_log_{SYMBOL}.csv")
STATE_FILE = os.getenv("STATE_FILE", f"state_{SYMBOL}.json")
