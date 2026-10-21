"""
Binance Testnetで実際に注文を出すデモトレード実行エンジン。

PaperTrader(内部シミュレーション)と同じインターフェース
(execute(signal, price) / portfolio_value(price) / snapshot())を持つため、
app.pyからはTRADING_MODE設定で透過的に切り替えられる。

【重要】「いくら使うか」の計算は、取引所の実際の口座残高ではなく、
DEMO_CAPITAL_USDT(自分の持ち金のつもり)を基準にした自前の帳簿(usdt_balance /
asset_balance)で行う。理由は2つ:
  1. Binance Testnetの口座には多額のテスト用資金が自動付与されるため、
     実際の残高をそのまま使うと本番運用の金額感と懸け離れてしまう
  2. 「売るときに実際の口座残高を丸ごと売る」実装だと、テスト用に最初から
     口座に入っていた資産まで一緒に売ってしまう事故が起きる(実際に発生した不具合)。
     自分が買った分だけを自前で記録し、それだけを売ることで防ぐ。

実際の注文(発注・約定)はBinance APIを通して本物を出すため、約定価格や
手数料相当のズレはそのまま帳簿に反映される。残高照会は「自前の帳簿が
実際の口座残高を超えていないか」の安全確認にのみ使う。

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
import time
from datetime import datetime, timezone

from config import (
    DEMO_CAPITAL_USDT,
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

    # ---------- 状態(自前の帳簿)の永続化 ----------

    def _default_state(self):
        return {
            "symbol": SYMBOL,
            "usdt_balance": DEMO_CAPITAL_USDT,
            "asset_balance": 0.0,
            "position": "NONE",
            "entry_price": None,
            "highest_price": None,
        }

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
            if not isinstance(state, dict):
                raise ValueError("invalid state")
            # 旧スキーマ(自前の残高を持たないバージョン)からの移行。
            # 旧バージョンは「口座残高を丸ごと売る」実装で金額の実体が無いため、
            # 自前の帳簿を前提にした新スキームでは素直にリセットする。
            if "usdt_balance" not in state or "asset_balance" not in state:
                logger.warning("デモトレードの状態が旧スキーマのため、初期状態(%.2f USDT)にリセットします", DEMO_CAPITAL_USDT)
                return self._default_state()
            if state.get("position") not in ("NONE", "LONG"):
                raise ValueError("invalid position")
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

    def _record_after_order(self, new_state, order_description):
        """
        取引所への発注が成功した「後」に、状態を記録する。

        PaperTraderと違い、DemoTraderは実際に外部(取引所)へ注文を出しており、
        約定してしまった注文は取り消せない。ここで永続化に失敗した場合に
        「取引所では約定済みなのに、ボット側はポジション無しと認識したまま」になると、
        次のシグナルで二重に買ってしまう等の不整合が起きる。

        そのため、メモリ上の状態は永続化の成否に関わらず必ず先に更新し(実際の約定結果に
        常に追従させる)、永続化は数回リトライする。それでも失敗した場合は、手動で
        復旧できるよう注文内容を含めて確実に通知する。
        """
        self.state = new_state
        text = json.dumps(new_state, indent=2)
        for attempt in range(3):
            try:
                self._write_raw(text)
                return
            except Exception as exc:
                logger.warning("[DEMO] 発注後の状態保存に失敗しました(試行%d/3): %s", attempt + 1, exc)
                if attempt < 2:
                    time.sleep(1)
        logger.error("[DEMO] 発注後の状態保存に3回失敗しました: %s", order_description)
        notify(
            f"🔥 [DEMO] 注文は成功しましたが、状態の保存に3回失敗しました。"
            f"このまま再起動すると、この注文の記録が失われます(手動での復旧が必要)。\n"
            f"注文内容: {order_description}\n保存したかった状態: {json.dumps(new_state)}",
            level="error", blocking=True,
        )

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
                        # 最高値の記録は取引ではなく補助的な更新。これが一時的に失敗しても、
                        # 以降のSL/TP/TS判定(ローカル変数のhighestを使う)まで巻き込んで
                        # 中断してはいけない(損切り判定のスキップにつながるため)。
                        try:
                            self._commit_state({**self.state, "highest_price": highest})
                        except Exception as exc:
                            logger.warning("[DEMO] 最高値の更新(永続化)に失敗しました。次サイクルで再試行します: %s", exc)

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
        spend = self.state["usdt_balance"] * TRADE_RATIO
        filters = binance_client.get_symbol_filters()
        if spend < filters["min_notional"]:
            message = f"HOLD: 自前残高不足のためBUYを見送り(発注予定額 {spend:.2f} < 最小注文額 {filters['min_notional']:.2f})"
            logger.warning("[DEMO] %s", message)
            return message

        # 安全確認: 自前の帳簿が実際の口座残高を超えて発注しないようにする
        # (本来ズレないはずだが、万一のズレで"残高不足"エラーになるのを防ぐための保険)。
        actual_usdt = binance_client.get_balance(_QUOTE_ASSET)
        if spend > actual_usdt:
            logger.warning(
                "[DEMO] 自前の帳簿(%.2f USDT)が実際の口座残高(%.2f USDT)を超えています。発注額を減額します",
                spend, actual_usdt,
            )
            spend = actual_usdt
            # 減額した結果、取引所の最小注文額を下回ると発注が拒否されてしまうので再確認する
            if spend < filters["min_notional"]:
                message = (
                    f"HOLD: 口座残高不足のためBUYを見送り"
                    f"(減額後の発注額 {spend:.2f} < 最小注文額 {filters['min_notional']:.2f})"
                )
                logger.warning("[DEMO] %s", message)
                return message

        order = binance_client.place_market_buy_by_quote(SYMBOL, spend)
        avg_price = order["avg_price"]
        executed_qty = float(order.get("executedQty", 0) or 0)
        quote_spent = float(order.get("cummulativeQuoteQty", 0) or 0)

        if executed_qty <= 0 or avg_price <= 0:
            # 発注自体は受理されたが約定していない(想定外)。この状態でポジションとして
            # 記録すると、entry_priceが0になりゼロ除算などの二次障害を起こすので記録しない。
            logger.error("[DEMO] BUY注文が約定していません: %s", order)
            notify(f"🚨 [DEMO] BUY注文が約定しませんでした(ポジションは記録していません): {order}", level="error")
            return "HOLD: BUY注文が約定せず(ポジションは記録していません)"

        new_state = dict(self.state)
        new_state["usdt_balance"] -= quote_spent
        new_state["asset_balance"] = executed_qty
        new_state["position"] = "LONG"
        new_state["entry_price"] = avg_price
        new_state["highest_price"] = avg_price
        self._record_after_order(new_state, f"BUY {executed_qty:.8f} {SYMBOL} @ {avg_price:,.2f}")

        message = f"[DEMO] BUY: {executed_qty:.6f} {SYMBOL} @ {avg_price:,.2f} (Binance Testnet)"
        notify(message, level="trade")
        return message

    def _sell(self, reason):
        # 自前の帳簿が記録している数量「だけ」を売る(実際の口座残高を丸ごと売らない)。
        qty = self.state["asset_balance"]
        filters = binance_client.get_symbol_filters()
        qty = binance_client.round_quantity(qty, filters["step_size"])

        # 安全確認: 自前の帳簿が実際の口座残高を超えて売らないようにする
        actual_balance = binance_client.get_balance(_BASE_ASSET)
        if qty > actual_balance:
            logger.warning(
                "[DEMO] 自前の帳簿(%.8f %s)が実際の口座残高(%.8f %s)を超えています。数量を減らします",
                qty, _BASE_ASSET, actual_balance, _BASE_ASSET,
            )
            qty = binance_client.round_quantity(actual_balance, filters["step_size"])

        if qty < filters["min_qty"]:
            message = f"HOLD: 自前残高不足のためSELLできません(数量 {qty})"
            logger.warning("[DEMO] %s", message)
            return message

        order = binance_client.place_market_sell(SYMBOL, qty)
        avg_price = order["avg_price"]
        executed_qty = float(order.get("executedQty", 0) or 0)
        quote_received = float(order.get("cummulativeQuoteQty", 0) or 0)

        if executed_qty <= 0:
            # 発注は受理されたが約定していない(想定外)。ポジションを「決済済み」として
            # 記録すると、実際には保有し続けているのに追跡が外れてしまうので記録しない。
            logger.error("[DEMO] SELL注文が約定していません: %s", order)
            notify(f"🚨 [DEMO] SELL注文が約定しませんでした(ポジションは保有中のまま): {order}", level="error")
            return "HOLD: SELL注文が約定せず(ポジションは保有中のまま)"

        entry = self.state["entry_price"] or avg_price
        cost = executed_qty * entry
        pnl = quote_received - cost
        pnl_pct = (pnl / cost * 100) if cost else 0.0

        new_state = dict(self.state)
        new_state["usdt_balance"] += quote_received
        new_state["asset_balance"] = 0.0
        new_state["position"] = "NONE"
        new_state["entry_price"] = None
        new_state["highest_price"] = None
        self._record_after_order(new_state, f"SELL {executed_qty:.8f} {SYMBOL} @ {avg_price:,.2f} ({reason})")

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
        with self._lock:
            return self.state["usdt_balance"] + self.state["asset_balance"] * current_price

    def snapshot(self):
        with self._lock:
            return dict(self.state)
