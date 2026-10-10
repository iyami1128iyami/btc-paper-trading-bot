"""
スレッド安全なペーパートレード実行エンジン。

状態(残高・ポジション)の保存先は、REDIS_URLが設定されていればRender Key Value(Redis)、
未設定ならローカルファイル(STATE_FILE)にフォールバックする。
Renderは再デプロイのたびにディスクを初期化するため、Redisを使わないと
デプロイをまたいでポジションが消えてしまう。
"""

import csv
import json
import logging
import math
import os
import threading
from datetime import datetime, timezone

from config import INITIAL_BALANCE_USDT, LOG_FILE, MIN_PROFIT_PCT_TO_EXIT, REDIS_URL, STATE_FILE, SYMBOL, TRADE_RATIO
from improved.notifier import notify

logger = logging.getLogger(__name__)

_redis_client = None
if REDIS_URL:
    import redis

    _redis_client = redis.from_url(
        REDIS_URL, decode_responses=True, socket_timeout=10, socket_connect_timeout=10
    )
    logger.info("状態の保存先: Render Key Value(Redis)")
else:
    logger.info("状態の保存先: ローカルファイル(%s) — REDIS_URL未設定のため再デプロイで消えます", STATE_FILE)


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

    # ---------- 低レベルの読み書き(Redis / ファイルを切り替え) ----------

    def _redis_key(self):
        return f"btc_trading_bot:state:{SYMBOL}"

    def _read_raw(self):
        """保存されている生のJSON文字列を返す。無ければNone。"""
        if _redis_client is not None:
            return _redis_client.get(self._redis_key())
        if not os.path.exists(STATE_FILE):
            return None
        with open(STATE_FILE, encoding="utf-8") as file:
            return file.read()

    def _write_raw(self, text: str):
        if _redis_client is not None:
            _redis_client.set(self._redis_key(), text)
            return
        directory = os.path.dirname(os.path.abspath(STATE_FILE)) or "."
        temporary_path = os.path.join(directory, f".{os.path.basename(STATE_FILE)}.tmp")
        with open(temporary_path, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, STATE_FILE)

    def _backup_corrupted_raw(self, raw):
        """破損データを退避する(調査用)。戻り値は退避先の説明文字列。"""
        timestamp = int(datetime.now(timezone.utc).timestamp())
        if _redis_client is not None:
            backup_key = f"{self._redis_key()}.corrupted.{timestamp}"
            try:
                _redis_client.set(backup_key, raw, ex=60 * 60 * 24 * 7)  # 7日で自動失効
                return backup_key
            except Exception:
                logger.exception("壊れた状態データの退避(Redis)に失敗しました")
                return "(退避失敗)"
        backup_path = f"{STATE_FILE}.corrupted.{timestamp}"
        try:
            with open(backup_path, "w", encoding="utf-8") as file:
                file.write(raw)
            return backup_path
        except OSError:
            logger.exception("壊れた状態ファイルの退避に失敗しました")
            return "(退避失敗)"

    # ---------- 状態の読み込み ----------

    def _load_state(self):
        try:
            raw = self._read_raw()
        except Exception as exc:
            # Redis接続自体に失敗した場合。永続化が効かない状態で起動を続けるのは
            # 「消えたことに気づかないまま運用する」最悪のケースなので、ここは
            # 明確に起動を止める(ファイルへのフォールバックは意図的に行わない)。
            logger.exception("状態ストアへの接続に失敗しました: %s", exc)
            notify(f"🔥 状態ストア(Redis)への接続に失敗しました。起動を中止します: {exc}", level="error")
            raise RuntimeError(f"状態ストアに接続できません: {exc}") from exc

        if raw is None:
            return self._default_state()

        try:
            state = json.loads(raw)
            state = self._validate_state(state)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            # 保存データの形式が壊れているケース。サービス全体を落とすより、
            # 壊れたデータを退避して初期状態で継続するほうが可用性としては適切。
            logger.exception("保存されている状態が壊れています。初期状態で再開します: %s", exc)
            backup_ref = self._backup_corrupted_raw(raw)
            notify(
                f"保存されている状態が壊れていたため、初期状態で再開しました。"
                f"破損データは {backup_ref} に退避済みです。内容を確認してください。",
                level="error",
            )
            return self._default_state()

        # 現在の設定(config.SYMBOL)と、保存されている状態のペアが食い違っていないか確認する。
        # これは「データ破損」とは違い、意図しない別ペアへの切り替えを防ぐ安全装置なので、
        # 検知した場合は復旧を試みずに明確に停止する。
        if state["symbol"] != SYMBOL:
            message = (
                f"保存されている状態のペア({state['symbol']})と現在の設定のペア({SYMBOL})が一致しません。"
                f"STATE_FILE/LOG_FILEを分けるか、意図的な切り替えなら保存データを削除してください。"
            )
            notify(f"起動を中止しました: {message}", level="error")
            raise RuntimeError(message)

        return state

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
        状態ストア(Redis/ファイル)への保存 → メモリ上の状態更新 → CSVログ記録、の順で行う。

        状態ストアを「残高の正(しょう)」として扱い、これの書き込みが失敗した場合は
        例外を投げてメモリも一切更新しない(取引はなかったことになる。安全側に倒す)。
        先にメモリを書き換えてから保存に失敗すると「メモリ上は取引済みだが
        永続化先には残っていない」状態になり、プロセス再起動時に取引が消えてしまうため、
        必ず保存の成功を確認してからメモリを更新する。

        CSVログはあくまで人間が見るための補助的な記録(ローカルファイルのまま)なので、
        これの書き込みに失敗しても取引自体は成立させる(警告ログのみ残す)。
        """
        self._write_raw(json.dumps(new_state, indent=2))
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
                    # 通常シグナルでの決済は、最低限の利益(往復手数料の目安)が
                    # 出ている場合のみ実行する。ストップロス/テイクプロフィットは
                    # 上のブロックで独立して判定済みなので、ここでの見送りが
                    # 損失の拡大には繋がらない(損切りラインに達すれば別途発動する)。
                    change = (price - self.entry_price) / self.entry_price * 100
                    if change >= MIN_PROFIT_PCT_TO_EXIT:
                        return self._execute_sell(price, "Signal")
                    logger.info(
                        "SELLシグナルが出ましたが、含み益(%.2f%%)がMIN_PROFIT_PCT_TO_EXIT(%.2f%%)"
                        "未満のため決済を見送ります",
                        change, MIN_PROFIT_PCT_TO_EXIT,
                    )
                    return f"HOLD: SELLシグナルだが利益不足({change:+.2f}%)のため見送り"
                return "HOLD: 何もしない"
            except Exception as exc:
                # ディスク書き込み失敗、Redis接続エラーなど。メモリ上の状態は
                # 書き換わっていないので帳簿は壊れていないが、取引自体は成立しなかった
                # ものとして呼び出し側に伝える。
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
