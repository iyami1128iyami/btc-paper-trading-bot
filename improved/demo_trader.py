"""
Binance Testnetで実際に注文を出すデモトレード実行エンジン。

PaperTrader(内部シミュレーション)と同じインターフェース
(execute(signal, price) / portfolio_value(price) / snapshot())を持つため、
app.pyからはTRADING_MODE設定で透過的に切り替えられる。

残高そのものはBinance Testnet側が「正(しょう)」であり、都度APIで問い合わせる。
一方「いくらで買ったか(entry_price)」「保有中の最高値(highest_price)」は
取引所側に概念が無いため、PaperTraderと同様に状態ストア(Redis/ファイル)で
自前管理する。

注意: ストップロス/テイクプロフィット/トレーリングストップ/最低利益チェックの
ロジックはPaperTraderと意図的に重複させている(1プロセスの実行パスを完全に分け、
片方の不具合がもう片方に影響しないようにするため)。ロジックを変更する際は
improved/paper_trader.py 側も合わせて更新すること。
"""

import json
import logging
import math
import os
import threading
from datetime import datetime, timezone

from config import (
    MIN_PROFIT_PCT_TO_EXIT,
    REDIS_URL,
    SYMBOL,
    TRADE_RATIO,
    TRAILING_STOP_ACTIVATION_PCT,
    TRAILING_STOP_PCT,
)
from improved import binance_client
from improved.notifier import notify

logger = logging.getLogger(__name__)

# SYMBOLは "BTCUSDT" のようなUSDT建てペアを前提にしている(他モジュールと同じ前提)。
_BASE_ASSET = SYMBOL[:-4] if SYMBOL.endswith("USDT") else SYMBOL
_QUOTE_ASSET = "USDT"

_STATE_FILE = f"demo_state_{SYMBOL}.json"

_redis_client = None
if REDIS_URL:
    import redis

    _redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=10, socket_connect_timeout=10)


class DemoTrader:
    def __init__(self, stop_loss_pct: float = 5.0, take_profit_pct: float = 10.0):
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self._lock = threading.RLock()
        self.state = self._load_state()

    # ---------- 状態(position/entry_price/highest_price)の永続化 ----------

    def _default_state(self):
        return {"symbol": SYMBOL, "position": "NONE", "entry_price": None, "highest_price": None}

    def _redis_key(self):
        return f"btc_trading_bot:demo_state:{SYMBOL}"

    def _read_raw(self):
        if _redis_client is not None:
            return _redis_client.get(self._redis_key())
        if not os.path.exists(_STATE_FILE):
            return None
        with open(_STATE_FILE, encoding="utf-8") as file:
            return file.read()

    def _write_raw(self, text: str):
        if _redis_client is not None:
            _redis_client.set(self._redis_key(), text)
            return
        directory = os.path.dirname(os.path.abspath(_STATE_FILE)) or "."
        temporary_path = os.path.join(directory, f".{os.path.basename(_STATE_FILE)}.tmp")
        with open(temporary_path, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, _STATE_FILE)

    def _load_state(self):
        try:
            raw = self._read_raw()
        except Exception as exc:
            logger.exception("デモトレード用の状態ストアへの接続に失敗しました: %s", exc)
            notify(f"🔥 デモトレード用の状態ストアへの接続に失敗しました。起動を中止します: {exc}",
                   level="error", blocking=True)
            raise RuntimeError(f"状態ストアに接続できません: {exc}") from exc

        if raw is None:
            return self._default_state()
        try:
            state = json.loads(raw)
            if not isinstance(state, dict) or state.get("position") not in ("NONE", "LONG"):
                raise ValueError("invalid state")
            state.setdefault("symbol", SYMBOL)
            state.setdefault("entry_price", None)
            state.setdefault("highest_price", None)
            if state["symbol"] != SYMBOL:
                raise ValueError(f"symbol mismatch: {state['symbol']} != {SYMBOL}")
            return state
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.exception("デモトレードの保存状態が壊れています。初期状態で再開します: %s", exc)
            notify(f"デモトレードの保存状態が壊れていたため、初期状態で再開しました: {exc}", level="error")
            return self._default_state()

    def _commit_state(self, new_state):
        self._write_raw(json.dumps(new_state, indent=2))
        self.state = new_state

    # ---------- 売買判定 ----------

    def execute(self, signal: str, price: float) -> str:
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            raise ValueError(f"price must be a finite positive number: {price}")
        with self._lock:
            try:
                if self.state["position"] == "LONG" and self.state.get("entry_price"):
                    entry = self.state["entry_price"]
                    highest = self.state.get("highest_price") or entry
                    if price > highest:
                        highest = price
                        self._commit_state({**self.state, "highest_price": highest})

                    change = (price - entry) / entry * 100
                    if change >= self.take_profit_pct:
                        return self._sell(f"TP {change:.2f}%")
                    if change <= -self.stop_loss_pct:
                        return self._sell(f"SL {change:.2f}%")

                    peak_change = (highest - entry) / entry * 100
                    if peak_change >= TRAILING_STOP_ACTIVATION_PCT:
                        drawdown = (highest - price) / highest * 100
                        if drawdown >= TRAILING_STOP_PCT:
                            return self._sell(f"TS -{drawdown:.2f}% (含み益{change:+.2f}%)")

                if signal == "BUY" and self.state["position"] == "NONE":
                    return self._buy()
                if signal == "SELL" and self.state["position"] == "LONG":
                    entry = self.state["entry_price"]
                    change = (price - entry) / entry * 100 if entry else 0
                    if change >= MIN_PROFIT_PCT_TO_EXIT:
                        return self._sell("Signal")
                    return f"HOLD: SELLシグナルだが利益不足({change:+.2f}%)のため見送り"
                return "HOLD: 何もしない"
            except Exception as exc:
                logger.exception("[DEMO] 取引の実行に失敗しました: %s", exc)
                notify(f"🚨 [DEMO] 取引の実行に失敗しました(Binance Testnet): {exc}", level="error")
                raise

    def _buy(self):
        usdt_balance = binance_client.get_balance(_QUOTE_ASSET)
        spend = usdt_balance * TRADE_RATIO
        filters = binance_client.get_symbol_filters()
        if spend < filters["min_notional"]:
            message = f"HOLD: 残高不足のためBUYを見送り(発注予定額 {spend:.2f} < 最小注文額 {filters['min_notional']:.2f})"
            logger.warning("[DEMO] %s", message)
            return message

        order = binance_client.place_market_buy_by_quote(SYMBOL, spend)
        avg_price = order["avg_price"]
        executed_qty = float(order.get("executedQty", 0) or 0)

        new_state = {"symbol": SYMBOL, "position": "LONG", "entry_price": avg_price, "highest_price": avg_price}
        self._commit_state(new_state)

        message = f"[DEMO] BUY: {executed_qty:.6f} {SYMBOL} @ {avg_price:,.2f} (Binance Testnet)"
        notify(message, level="trade")
        return message

    def _sell(self, reason):
        balance = binance_client.get_balance(_BASE_ASSET)
        filters = binance_client.get_symbol_filters()
        qty = binance_client.round_quantity(balance, filters["step_size"])
        if qty < filters["min_qty"]:
            message = f"HOLD: 残高不足のためSELLできません({_BASE_ASSET}残高 {balance})"
            logger.warning("[DEMO] %s", message)
            return message

        order = binance_client.place_market_sell(SYMBOL, qty)
        avg_price = order["avg_price"]
        executed_qty = float(order.get("executedQty", 0) or 0)
        quote_received = float(order.get("cummulativeQuoteQty", 0) or 0)

        entry = self.state["entry_price"] or avg_price
        cost = executed_qty * entry
        pnl = quote_received - cost
        pnl_pct = (pnl / cost * 100) if cost else 0.0

        new_state = {"symbol": SYMBOL, "position": "NONE", "entry_price": None, "highest_price": None}
        self._commit_state(new_state)

        message = (
            f"[DEMO] SELL: {executed_qty:.6f} {SYMBOL} @ {avg_price:,.2f} ({reason}) "
            f"損益: {pnl:+,.2f} USDT ({pnl_pct:+.2f}%) (Binance Testnet)"
        )
        notify(message, level="trade")
        logger.info("[DEMO] SELL P&L: %.2f (%s)", pnl, reason)
        return message

    def portfolio_value(self, current_price: float) -> float:
        if not isinstance(current_price, (int, float)) or not math.isfinite(current_price) or current_price < 0:
            raise ValueError("current_price must be finite and non-negative")
        usdt = binance_client.get_balance(_QUOTE_ASSET)
        asset = binance_client.get_balance(_BASE_ASSET)
        return usdt + asset * current_price

    def snapshot(self):
        with self._lock:
            return dict(self.state)
