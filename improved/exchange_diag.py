"""
取引所の公開API(認証不要)に、このサーバーから到達できるかを確認する診断。

本番口座を作る前に、「海外(Renderのリージョン)のサーバーからでも、その取引所の
APIが使えるか」を確かめるためのもの。以前、米国リージョンからBinanceに451エラーで
ブロックされた経験があり、国内取引所にも同様の制限がある可能性があるため。

RUN_EXCHANGE_DIAG=1 を設定して起動したときだけ、起動時に1回実行する(通常は何もしない)。
結果はログとTelegram通知に出す。

注意:
- 公開API(価格取得)に届くことは、注文などの非公開APIにも届くことを保証しない。
  あくまで「口座を作る前の第一段階の確認」。
- 今後本番で使うAPIクライアントと同じ条件で確認するため、User-Agentなどは
  requestsの既定のまま(診断用に偽装しない)。
- Binance Japan独自のAPIドメインは未確認のため、ここでは対象に含めていない
  (api.binance.comへの到達は、あくまで参考値)。
"""

import logging
import threading
import time

import requests

from improved.notifier import notify

logger = logging.getLogger(__name__)

# (表示名, URL)。いずれも認証不要のGET。
_TARGETS = [
    ("CoinGecko(対照: 既に動いている通信)", "https://api.coingecko.com/api/v3/ping"),
    ("Binance(api.binance.com・参考)", "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"),
    ("bitFlyer", "https://api.bitflyer.com/v1/getticker?product_code=BTC_JPY"),
    ("GMOコイン", "https://api.coin.z.com/public/v1/ticker?symbol=BTC"),
    ("bitbank", "https://public.bitbank.cc/btc_jpy/ticker"),
]

_BLOCK_STATUSES = (403, 451)


def check_one(name: str, url: str, timeout: float = 10.0) -> dict:
    """1か所のURLに到達できるかを確認する。例外は握りつぶさず、結果の一部として返す。"""
    started = time.monotonic()
    try:
        resp = requests.get(url, timeout=timeout)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {
            "name": name,
            "url": url,
            "ok": resp.status_code == 200,
            "status": resp.status_code,
            "elapsed_ms": elapsed_ms,
            "detail": resp.text[:100].replace("\n", " "),
        }
    except Exception as exc:  # 診断なので、どんな失敗でも結果として記録して次へ進む
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {
            "name": name,
            "url": url,
            "ok": False,
            "status": None,
            "elapsed_ms": elapsed_ms,
            "detail": f"{type(exc).__name__}: {exc}"[:150],
        }


def run_diagnostics() -> list[dict]:
    results = []
    for name, url in _TARGETS:
        result = check_one(name, url)
        results.append(result)
        logger.info(
            "[DIAG] %s: %s status=%s %dms detail=%s",
            result["name"], "OK" if result["ok"] else "NG",
            result["status"], result["elapsed_ms"], result["detail"],
        )
    return results


def format_summary(results: list[dict]) -> str:
    lines = ["取引所APIへの到達確認(公開API・このサーバーから)"]
    for r in results:
        if r["ok"]:
            lines.append(f"✅ {r['name']}: 到達OK (HTTP 200, {r['elapsed_ms']}ms)")
        elif r["status"] in _BLOCK_STATUSES:
            lines.append(f"❌ {r['name']}: アクセス拒否の可能性 (HTTP {r['status']})")
        elif r["status"] is not None:
            lines.append(f"⚠️ {r['name']}: HTTP {r['status']} {r['detail'][:60]}")
        else:
            lines.append(f"❌ {r['name']}: 接続できず ({r['detail'][:80]})")
    lines.append("※公開APIに届いても、注文などの非公開APIも使えるとは限りません")
    return "\n".join(lines)


def run_diagnostics_in_background() -> threading.Thread:
    """起動を遅らせないよう、別スレッドで実行して結果をログとTelegramに出す。"""

    def _worker():
        try:
            results = run_diagnostics()
            notify(format_summary(results), level="info")
        except Exception:
            logger.exception("[DIAG] 診断の実行に失敗しました")

    thread = threading.Thread(target=_worker, daemon=True, name="exchange-diag")
    thread.start()
    return thread
