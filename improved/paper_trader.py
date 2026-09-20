"""
改善版：ペーパートレード実行エンジン

スレッド安全性を追加：
- Lock機構で状態の一貫性を保証
- ストップロス/テイクプロフィット機能
"""

import csv
import json
import logging
import os
import threading
from datetime import datetime, timezone

from config import INITIAL_BALANCE_USDT, LOG_FILE, STATE_FILE, TRADE_RATIO

logger = logging.getLogger(__name__)


class PaperTrader:
    def __init__(self, stop_loss_pct: float = 5.0, take_profit_pct: float = 10.0):
        self._lock = threading.Lock()
        self.state = self._load_state()
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.entry_price = None
        self._init_log_file()

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"状態ファイル読み込み失敗: {e}. 初期化します")

        return {
            "usdt_balance": INITIAL_BALANCE_USDT,
            "btc_balance": 0.0,
            "position": "NONE",
        }

    def _save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"状態ファイル保存失敗: {e}")
            raise

    def _init_log_file(self):
        if not os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(
                        [
                            "timestamp",
                            "action",
                            "price",
                            "amount_btc",
                            "usdt_balance",
                            "btc_balance",
                            "reason",
                        ]
                    )
            except Exception as e:
                logger.error(f"ログファイル初期化失敗: {e}")

    def _log_trade(self, action: str, price: float, amount_btc: float, reason: str = ""):
        try:
            with open(LOG_FILE, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        datetime.now(timezone.utc).isoformat(),
                        action,
                        round(price, 2),
                        round(amount_btc, 8),
                        round(self.state["usdt_balance"], 2),
                        round(self.state["btc_balance"], 8),
                        reason,
                    ]
                )
        except Exception as e:
            logger.error(f"ログ記録失敗: {e}")

    def execute(self, signal: str, price: float) -> str:
        with self._lock:
            if self.state["position"] == "LONG" and self.entry_price:
                price_change_pct = ((price - self.entry_price) / self.entry_price) * 100

                if price_change_pct >= self.take_profit_pct:
                    logger.info(f"テイクプロフィット発動: {price_change_pct:.2f}%")
                    return self._execute_sell(price, f"TP {price_change_pct:.2f}%")

                if price_change_pct <= -self.stop_loss_pct:
                    logger.warning(f"ストップロス発動: {price_change_pct:.2f}%")
                    return self._execute_sell(price, f"SL {price_change_pct:.2f}%")

            if signal == "BUY" and self.state["position"] == "NONE":
                return self._execute_buy(price, "Signal")
            if signal == "SELL" and self.state["position"] == "LONG":
                return self._execute_sell(price, "Signal")
            return "HOLD: 何もしない"

    def _execute_buy(self, price: float, reason: str) -> str:
        spend_usdt = self.state["usdt_balance"] * TRADE_RATIO
        amount_btc = spend_usdt / price

        self.state["usdt_balance"] -= spend_usdt
        self.state["btc_balance"] += amount_btc
        self.state["position"] = "LONG"
        self.entry_price = price

        self._log_trade("BUY", price, amount_btc, reason)
        self._save_state()

        logger.info(f"BUY実行: {amount_btc:.6f} BTC @ {price:.2f} USDT ({reason})")
        return f"BUY: {amount_btc:.6f} BTC @ {price:.2f} USDT"

    def _execute_sell(self, price: float, reason: str) -> str:
        amount_btc = self.state["btc_balance"]
        gain_usdt = amount_btc * price
        pnl = gain_usdt - (self.state["usdt_balance"] * TRADE_RATIO)

        self.state["usdt_balance"] += gain_usdt
        self.state["btc_balance"] = 0.0
        self.state["position"] = "NONE"
        self.entry_price = None

        self._log_trade("SELL", price, amount_btc, reason)
        self._save_state()

        logger.info(f"SELL実行: {amount_btc:.6f} BTC @ {price:.2f} USDT | P&L: {pnl:.2f} ({reason})")
        return f"SELL: {amount_btc:.6f} BTC @ {price:.2f} USDT"

    def portfolio_value(self, current_price: float) -> float:
        """現在の評価総資産(USDT換算)を計算する"""
        with self._lock:
            return self.state["usdt_balance"] + self.state["btc_balance"] * current_price
