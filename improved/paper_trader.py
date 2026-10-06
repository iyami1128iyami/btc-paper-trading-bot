"""スレッド安全なペーパートレード実行エンジン。"""

import csv
import json
import logging
import math
import os
import shutil
import threading
from datetime import datetime, timezone

from config import INITIAL_BALANCE_USDT, LOG_FILE, STATE_FILE, SYMBOL, TRADE_RATIO
from improved.notifier import notify

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
        return {
            "symbol": SYMBOL,
            "usdt_balance": INITIAL_BALANCE_USDT,
            "asset_balance": 0.0,
            "position": "NONE",
            "entry_price": None,
        }

    def _validate_state(self, state):
        """読み込んだ状態の形式チェック。問題があればValueErrorを投げる。"""
        if not isinstance(state, dict):
            raise ValueError("state must be an object")
        if "asset_balance" not in state and "btc_balance" in state:
            state["asset_balance"] = state.pop("btc_balance")
        state.setdefault("symbol", SYMBOL)
        state.setdefault("entry_price", None)

        if state.get("position") not in {"NONE", "LONG"}:
            raise ValueError("invalid position")
        for key in ("usdt_balance", "asset_balance"):
            value = state.get(key)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"invalid {key}")
        entry_price = state["entry_price"]
        if entry_price is not None and (
            not isinstance(entry_price, (int, float)) or not math.isfinite(entry_price) or entry_price <= 0
        ):
            raise ValueError("invalid entry_price")
        if state["position"] == "LONG" and entry_price is None:
            raise ValueError("LONG position requires entry_price")
        return state

    def _load_state(self):
        if not os.path.exists(STATE_FILE):
            return self._default_state()

        try:
            with open(STATE_FILE, encoding="utf-8") as file:
                state = json.load(file)
            state = self._validate_state(state)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            # ここは「ファイルが物理的に壊れている/形式が不正」なケース。
            # サービス全体を落とすより、壊れたファイルを退避して初期状態で継続するほうが
            # ペーパートレードの可用性としては適切なので、ここでは起動を止めない。
            logger.exception("状態ファイルが壊れています。初期状態で再開します: %s", exc)
            backup_path = f"{STATE_FILE}.corrupted.{int(datetime.now(timezone.utc).timestamp())}"
            try:
                shutil.move(STATE_FILE, backup_path)
            except OSError:
                logger.exception("壊れた状態ファイルの退避に失敗しました: %s", STATE_FILE)
            notify(
                f"状態ファイル({STATE_FILE})が壊れていたため、初期状態で再開しました。"
                f"破損ファイルは {backup_path} に退避済みです。内容を確認してください。",
                level="error",
            )
            return self._default_state()

        # 現在の設定(config.SYMBOL)と、保存されている状態のペアが食い違っていないか確認する。
        # これは「ファイル破損」とは違い、意図しない別ペアへの切り替えを防ぐ安全装置なので、
        # 検知した場合は復旧を試みずに明確に停止する。
        if state["symbol"] != SYMBOL:
            message = (
                f"状態ファイルのペア({state['symbol']})と現在の設定のペア({SYMBOL})が一致しません。"
                f"STATE_FILE/LOG_FILEを分けるか、意図的な切り替えならファイルを削除してください。"
            )
            notify(f"起動を中止しました: {message}", level="error")
            raise RuntimeError(message)

        return state

    def _atomic_write_json(self, path, data):
        directory = os.path.dirname(os.path.abspath(path)) or "."
        temporary_path = os.path.join(directory, f".{os.path.basename(path)}.tmp")
        with open(temporary_path, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)

    def _init_log_file(self):
        if not os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", newline="", encoding="utf-8") as file:
                csv.writer(file).writerow(
                    ["timestamp", "symbol", "action", "price", "amount", "usdt_balance", "asset_balance", "reason"]
                )

    def _append_log_row(self, new_state, action, price, amount, reason):
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow([
                datetime.now(timezone.utc).isoformat(), SYMBOL, action, round(price, 2), round(amount, 8),
                round(new_state["usdt_balance"], 2), round(new_state["asset_balance"], 8), reason,
            ])

    def _commit(self, new_state, new_entry_price, action, price, amount, reason):
        """
        state.json への保存 → メモリ上の状態更新 → CSVログ記録、の順で行う。

        state.jsonを「残高の正(しょう)」として扱い、これの書き込みが失敗した場合は
        例外を投げてメモリも一切更新しない(取引はなかったことになる。安全側に倒す)。
        先にメモリを書き換えてから保存に失敗すると「メモリ上は取引済みだが
        ファイルには残っていない」状態になり、プロセス再起動時に取引が消えてしまうため、
        必ずファイル保存の成功を確認してからメモリを更新する。

        CSVログはあくまで人間が見るための補助的な記録なので、これの書き込みに
        失敗しても取引自体は成立させる(警告ログのみ残す)。
        """
        self._atomic_write_json(STATE_FILE, new_state)
        self.state = new_state
        self.entry_price = new_entry_price
        try:
            self._append_log_row(new_state, action, price, amount, reason)
        except OSError as exc:
            logger.warning("取引は成立しましたが、CSVログへの記録に失敗しました: %s", exc)

    def execute(self, signal: str, price: float) -> str:
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            raise ValueError(f"price must be a finite positive number: {price}")
        with self._lock:
            try:
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
            except OSError as exc:
                # ディスク書き込み失敗など。メモリ上の状態は書き換わっていないので
                # 帳簿は壊れていないが、取引自体は成立しなかったものとして呼び出し側に伝える。
                logger.exception("取引の記録に失敗しました(取引は成立させていません): %s", exc)
                notify(f"取引の記録に失敗しました。この取引はスキップされました: {exc}", level="error")
                raise

    def _execute_buy(self, price, reason):
        spend = self.state["usdt_balance"] * TRADE_RATIO
        amount = spend / price
        new_state = dict(self.state)
        new_state["usdt_balance"] -= spend
        new_state["asset_balance"] += amount
        new_state["position"] = "LONG"
        new_state["entry_price"] = price

        self._commit(new_state, price, "BUY", price, amount, reason)
        message = f"BUY: {amount:.6f} {SYMBOL} @ {price:,.2f} ({reason})"
        notify(message, level="trade")
        return message

    def _execute_sell(self, price, reason):
        amount = self.state["asset_balance"]
        gain = amount * price
        cost = amount * self.entry_price
        pnl = gain - cost
        pnl_pct = (pnl / cost * 100) if cost else 0.0

        new_state = dict(self.state)
        new_state["usdt_balance"] += gain
        new_state["asset_balance"] = 0.0
        new_state["position"] = "NONE"
        new_state["entry_price"] = None

        self._commit(new_state, None, "SELL", price, amount, reason)
        message = (
            f"SELL: {amount:.6f} {SYMBOL} @ {price:,.2f} ({reason}) "
            f"損益: {pnl:+,.2f} USDT ({pnl_pct:+.2f}%)"
        )
        notify(message, level="trade")
        logger.info("SELL P&L: %.2f (%s)", pnl, reason)
        return message

    def portfolio_value(self, current_price: float) -> float:
        if not isinstance(current_price, (int, float)) or not math.isfinite(current_price) or current_price < 0:
            raise ValueError("current_price must be finite and non-negative")
        with self._lock:
            return self.state["usdt_balance"] + self.state["asset_balance"] * current_price

    def snapshot(self):
        with self._lock:
            return dict(self.state)
