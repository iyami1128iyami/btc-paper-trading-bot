"""
Binance Spot Testnetの認証付きAPIクライアント。

HMAC-SHA256署名を使った注文発注・残高照会を行う。実際の取引所APIにつなぐ
最初のステップとして、まずはテストネット(仮想資金)で動作検証する。
BINANCE_BASE_URLを本番用に変更すれば、同じコードで本番環境にも接続できる
(API形式がテストネットと本番でほぼ共通であるBinanceの特性を利用している)。
"""

import hashlib
import hmac
import logging
import math
import time
import urllib.parse

import requests

from config import BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_BASE_URL, SYMBOL

logger = logging.getLogger(__name__)

_session = requests.Session()
if BINANCE_API_KEY:
    _session.headers.update({"X-MBX-APIKEY": BINANCE_API_KEY})

_symbol_filters_cache: dict | None = None


class BinanceAPIError(Exception):
    """Binance APIの呼び出し失敗(認証エラー・注文拒否・通信エラーなど)。"""


def _sign(params: dict) -> str:
    query = urllib.parse.urlencode(params)
    return hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()


def _signed_request(method: str, path: str, params: dict | None = None, retries: int = 3,
                     backoff_factor: float = 2.0) -> dict:
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        raise BinanceAPIError("BINANCE_API_KEY / BINANCE_API_SECRET が設定されていません")

    for attempt in range(retries):
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 10000
        params["signature"] = _sign(params)
        url = f"{BINANCE_BASE_URL}{path}"
        try:
            resp = _session.request(method, url, params=params, timeout=10)
            if resp.status_code in (429, 418):
                retry_after = int(resp.headers.get("Retry-After", "5") or "5")
                logger.warning("Binance APIのレート制限に達しました。%s秒待機します", retry_after)
                if attempt < retries - 1:
                    time.sleep(retry_after)
                    continue
            if resp.status_code >= 400:
                # Binanceはエラー時も {"code": ..., "msg": ...} 形式のJSONを返すことが多いので、
                # 可能な範囲でその内容を例外メッセージに含める(デバッグしやすくするため)。
                try:
                    detail = resp.json()
                except ValueError:
                    detail = resp.text
                raise BinanceAPIError(f"Binance APIエラー(HTTP {resp.status_code}): {detail}")
            return resp.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if attempt >= retries - 1:
                logger.error("Binance APIへの接続に失敗しました: %s", exc)
                raise BinanceAPIError(str(exc)) from exc
            wait = backoff_factor**attempt
            logger.warning("Binance API接続エラー。%.1f秒待機してリトライします: %s", wait, exc)
            time.sleep(wait)
    raise BinanceAPIError("リトライ上限に達しました")


def _public_request(path: str, params: dict | None = None, retries: int = 3) -> dict:
    url = f"{BINANCE_BASE_URL}{path}"
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as exc:
            if attempt >= retries - 1:
                raise BinanceAPIError(str(exc)) from exc
            time.sleep(2**attempt)


def get_symbol_filters(symbol: str = SYMBOL) -> dict:
    """
    最小注文単位(LOT_SIZE)や最小注文金額(MIN_NOTIONAL)などの制約を取得する。
    一度取得した内容はプロセス内でキャッシュする(symbolごとの制約は滅多に変わらないため)。
    """
    global _symbol_filters_cache
    if _symbol_filters_cache is not None and _symbol_filters_cache.get("symbol") == symbol:
        return _symbol_filters_cache

    data = _public_request("/api/v3/exchangeInfo", {"symbol": symbol})
    symbols = data.get("symbols") or []
    if not symbols:
        raise BinanceAPIError(f"銘柄情報が取得できません: {symbol}")

    filters = {f["filterType"]: f for f in symbols[0].get("filters", [])}
    lot_size = filters.get("LOT_SIZE", {})
    notional = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
    result = {
        "symbol": symbol,
        "step_size": float(lot_size.get("stepSize", 0) or 0),
        "min_qty": float(lot_size.get("minQty", 0) or 0),
        "min_notional": float(notional.get("minNotional", 0) or 0),
    }
    _symbol_filters_cache = result
    return result


def round_quantity(quantity: float, step_size: float) -> float:
    """注文数量を、取引所が許可する最小単位(step_size)の倍数に切り捨てる。"""
    if step_size <= 0 or quantity <= 0:
        return max(quantity, 0.0)
    precision = max(0, int(round(-math.log10(step_size))))
    adjusted = math.floor(quantity / step_size) * step_size
    return round(adjusted, precision)


def get_balance(asset: str) -> float:
    """指定資産の利用可能残高(発注に使える分、ロックされていない分)を取得する。"""
    data = _signed_request("GET", "/api/v3/account")
    for balance in data.get("balances", []):
        if balance["asset"] == asset:
            return float(balance["free"])
    return 0.0


def _average_fill_price(order: dict) -> float:
    """注文レスポンスから平均約定価格を計算する。"""
    executed_qty = float(order.get("executedQty", 0) or 0)
    quote_qty = float(order.get("cummulativeQuoteQty", 0) or 0)
    if executed_qty <= 0:
        return 0.0
    return quote_qty / executed_qty


def place_market_buy_by_quote(symbol: str, quote_amount: float) -> dict:
    """
    成行の買い注文を、USDTなどの「使う金額」で指定して発注する。
    (BTC数量の計算を取引所側に任せられるので、こちらで価格を見積もる必要がない)
    """
    order = _signed_request(
        "POST", "/api/v3/order",
        {"symbol": symbol, "side": "BUY", "type": "MARKET", "quoteOrderQty": f"{quote_amount:.8f}"},
    )
    order["avg_price"] = _average_fill_price(order)
    return order


def place_market_sell(symbol: str, quantity: float) -> dict:
    """成行の売り注文を、売却する数量を指定して発注する。"""
    order = _signed_request(
        "POST", "/api/v3/order",
        {"symbol": symbol, "side": "SELL", "type": "MARKET", "quantity": f"{quantity:.8f}"},
    )
    order["avg_price"] = _average_fill_price(order)
    return order
