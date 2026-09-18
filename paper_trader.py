"""
ペーパートレード実行エンジン

実際のお金は動かさず、仮想の残高・ポジションを管理して
売買シグナルに従って「買った/売ったつもり」で記録していく。
"""

import json
import csv
import os
from datetime import datetime, timezone

from config import (
    INITIAL_BALANCE_USDT,
    TRADE_RATIO,
    LOG_FILE,
    STATE_FILE,
)


class PaperTrader:
    def __init__(self):
        self.state = self._load_state()
        self._init_log_file()

    # ---------- 状態の読み書き ----------

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        # 初回起動時の初期状態
        return {
            "usdt_balance": INITIAL_BALANCE_USDT,
            "btc_balance": 0.0,
            "position": "NONE",  # NONE または LONG
        }

    def _save_state(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2)

    def _init_log_file(self):
        if not os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(
                    ["timestamp", "action", "price", "amount_btc", "usdt_balance", "btc_balance"]
                )

    def _log_trade(self, action, price, amount_btc):
        with open(LOG_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    datetime.now(timezone.utc).isoformat(),
                    action,
                    price,
                    amount_btc,
                    round(self.state["usdt_balance"], 2),
                    round(self.state["btc_balance"], 8),
                ]
            )

    # ---------- 売買処理 ----------

    def execute(self, signal: str, price: float):
        """
        シグナルに応じて仮想の売買を実行する。
        BUY: ポジションを持っていない時だけ買う
        SELL: ポジションを持っている時だけ売る
        """
        if signal == "BUY" and self.state["position"] == "NONE":
            spend_usdt = self.state["usdt_balance"] * TRADE_RATIO
            amount_btc = spend_usdt / price

            self.state["usdt_balance"] -= spend_usdt
            self.state["btc_balance"] += amount_btc
            self.state["position"] = "LONG"

            self._log_trade("BUY", price, amount_btc)
            self._save_state()
            return f"BUY: {amount_btc:.6f} BTC @ {price:.2f} USDT"

        elif signal == "SELL" and self.state["position"] == "LONG":
            amount_btc = self.state["btc_balance"]
            gain_usdt = amount_btc * price

            self.state["usdt_balance"] += gain_usdt
            self.state["btc_balance"] = 0.0
            self.state["position"] = "NONE"

            self._log_trade("SELL", price, amount_btc)
            self._save_state()
            return f"SELL: {amount_btc:.6f} BTC @ {price:.2f} USDT"

        else:
            return "HOLD: 何もしない"

    def portfolio_value(self, current_price: float) -> float:
        """現在の評価総資產(USDT換算)を計算する"""
        return self.state["usdt_balance"] + self.state["btc_balance"] * current_price
