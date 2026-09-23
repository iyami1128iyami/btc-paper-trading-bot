"""スレッド安全なペーパートレード実行エンジン。"""

import csv
import json
import logging
import math
import os
import threading
from datetime import datetime, timezone

from config import INITIAL_BALANCE_USDT, LOG_FILE, STATE_FILE, TRADE_RATIO

logger = logging.getLogger(__name__)


class PaperTrader:
    def __init__(self, stop_loss_pct: float = 5.0, take_profit_pct: float = 10.0):
        if not 0 <= TRADE_RATIO <= 1:
            raise ValueError("TRADE_RATIO must be between 0 and 1")
        self._lock = threading.RLock()
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.state = self._load_state()
        self.entry_price = self.state.get("entry_price")
        self._init_log_file()

    def _default_state(self):
        return {"usdt_balance": INITIAL_BALANCE_USDT, "btc_balance": 0.0, "position": "NONE", "entry_price": None}

    def _load_state(self):
        if not os.path.exists(STATE_FILE):
            return self._default_state()
        try:
            with open(STATE_FILE, encoding="utf-8") as file:
                state = json.load(file)
            if not isinstance(state, dict):
                raise ValueError("state must be an object")
            state.setdefault("entry_price", None)
            if state.get("position") not in {"NONE", "LONG"}:
                raise ValueError("invalid position")
            for key in ("usdt_balance", "btc_balance"):
                value = state[key]
                if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError(f"invalid {key}")
            entry_price = state["entry_price"]
            if entry_price is not None and (not isinstance(entry_price, (int, float)) or not math.isfinite(entry_price) or entry_price <= 0):
                raise ValueError("invalid entry_price")
            if state["position"] == "LONG" and entry_price is None:
                raise ValueError("LONG position requires entry_price")
            return state
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.exception("状態ファイルを読み込めません: %s", exc)
            raise RuntimeError(f"invalid state file: {STATE_FILE}") from exc

    def _save_state(self):
        directory = os.path.dirname(os.path.abspath(STATE_FILE)) or "."
        temporary_path = os.path.join(directory, f".{os.path.basename(STATE_FILE)}.tmp")
        try:
            with open(temporary_path, "w", encoding="utf-8") as file:
                json.dump(self.state, file, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, STATE_FILE)
        except OSError:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)
            raise

    def _init_log_file(self):
        if not os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", newline="", encoding="utf-8") as file:
                csv.writer(file).writerow(["timestamp", "action", "price", "amount_btc", "usdt_balance", "btc_balance", "reason"])

    def _log_trade(self, action, price, amount_btc, reason=""):
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow([
                datetime.now(timezone.utc).isoformat(), action, round(price, 2), round(amount_btc, 8),
                round(self.state["usdt_balance"], 2), round(self.state["btc_balance"], 8), reason,
            ])

    def execute(self, signal: str, price: float) -> str:
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            raise ValueError(f"price must be a finite positive number: {price}")
        with self._lock:
            if self.state["position"] == "LONG" and self.entry_price is not None:
                change = (price - self.entry_price) / self.entry_price * 100
                if change >= self.take_profit_pct:
                    return self._execute_sell(price, f"TP {change:.2f}%")
                if change <= -self.stop_loss_pct:
                    return self._execute_sell(price, f"SL {change:.2f}%")
            if signal == "BUY" and self.state["position"] == "NONE":
                return self._execute_buy(price, "Signal")
            if signal == "SELL" and self.state["position"] == "LONG":
                return self._execute_sell(price, "Signal")
            return "HOLD: 何もしない"

    def _execute_buy(self, price, reason):
        spend = self.state["usdt_balance"] * TRADE_RATIO
        amount = spend / price
        self.state["usdt_balance"] -= spend
        self.state["btc_balance"] += amount
        self.state["position"] = "LONG"
        self.entry_price = price
        self.state["entry_price"] = price
        self._log_trade("BUY", price, amount, reason)
        self._save_state()
        return f"BUY: {amount:.6f} BTC @ {price:.2f} USDT"

    def _execute_sell(self, price, reason):
        amount = self.state["btc_balance"]
        gain = amount * price
        cost = amount * self.entry_price
        pnl = gain - cost
        self.state["usdt_balance"] += gain
        self.state["btc_balance"] = 0.0
        self.state["position"] = "NONE"
        self.entry_price = None
        self.state["entry_price"] = None
        self._log_trade("SELL", price, amount, reason)
        self._save_state()
        logger.info("SELL P&L: %.2f (%s)", pnl, reason)
        return f"SELL: {amount:.6f} BTC @ {price:.2f} USDT"

    def portfolio_value(self, current_price: float) -> float:
        if not isinstance(current_price, (int, float)) or not math.isfinite(current_price) or current_price < 0:
            raise ValueError("current_price must be finite and non-negative")
        with self._lock:
            return self.state["usdt_balance"] + self.state["btc_balance"] * current_price

    def snapshot(self):
        with self._lock:
            return dict(self.state)
