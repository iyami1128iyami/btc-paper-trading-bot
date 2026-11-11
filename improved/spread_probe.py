"""
国内取引所(bitFlyer / GMOコイン / bitbank)のBTC/JPYの売買差(スプレッド)を、
このサーバーから実測する。

成行注文で買って売ると、手数料のほかに「売値と買値の差(スプレッド)」がそのまま
実質的なコストになる。取引所を決める前に、手数料だけでなく売買差も含めた
往復コストを比べるための測定。

SPREAD_PROBE_MINUTES に1以上を設定して起動すると、その分数のあいだ一定間隔で
各社の最良売値・最良買値を取得し、終了時に統計をログとTelegramに出す(0なら何もしない)。

注意:
- 売買差は時間帯や相場の荒さで変わる。1回の測定は「その時間帯の姿」でしかないので、
  別の時間帯にも測るとより確実。
- 認証不要の公開APIのみを使う(注文や口座には一切触れない)。
- Taker手数料は2026年10月時点の調査値。口座開設前に各社の公式ページで確認すること。
"""

import logging
import threading
import time

import requests

from improved.notifier import notify

logger = logging.getLogger(__name__)

_INTERVAL_SEC = 15

# 成行注文(Taker)の手数料率(%)。2026年10月時点の調査値で、取引量や改定で変わりうる。
#  - bitFlyer: 直近30日の取引量が少ない場合の最大料率(取引量が増えると下がる)
#  - GMOコイン: 複数の情報源で一致(公式ページは未確認)
#  - bitbank: 2026/2/2改定後の公式お知らせ
_TAKER_FEE_PCT = {"bitFlyer": 0.15, "GMOコイン": 0.05, "bitbank": 0.10}


def _fetch_bitflyer() -> tuple[float, float]:
    resp = requests.get(
        "https://api.bitflyer.com/v1/getticker", params={"product_code": "BTC_JPY"}, timeout=10
    )
    resp.raise_for_status()
    data = resp.json()
    return float(data["best_bid"]), float(data["best_ask"])


def _fetch_gmo() -> tuple[float, float]:
    # symbol=BTC は取引所(現物)。レバレッジ取引はBTC_JPY。
    resp = requests.get("https://api.coin.z.com/public/v1/ticker", params={"symbol": "BTC"}, timeout=10)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != 0:
        raise ValueError(f"GMOコインAPIがエラーを返しました: {payload.get('messages')}")
    data = payload["data"]
    item = data[0] if isinstance(data, list) else data
    return float(item["bid"]), float(item["ask"])


def _fetch_bitbank() -> tuple[float, float]:
    resp = requests.get("https://public.bitbank.cc/btc_jpy/ticker", timeout=10)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("success") != 1:
        raise ValueError(f"bitbank APIがエラーを返しました: {payload.get('data')}")
    data = payload["data"]
    # bitbankの "buy" は最良買値(bid)、"sell" は最良売値(ask)
    return float(data["buy"]), float(data["sell"])


_SOURCES = {
    "bitFlyer": _fetch_bitflyer,
    "GMOコイン": _fetch_gmo,
    "bitbank": _fetch_bitbank,
}


def _describe_error(exc: Exception) -> str:
    """失敗の原因が分かる短い文字列にする(型名だけでなくHTTPステータスと応答の先頭も出す)。"""
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            body = (response.text or "").strip().replace("\n", " ")[:80]
        except Exception:
            body = ""
        return f"{type(exc).__name__} status={response.status_code} body={body!r}"
    return f"{type(exc).__name__}: {str(exc)[:80]}"


def _spread_pct(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 100


def collect_samples(rounds: int, interval_sec: float = _INTERVAL_SEC):
    """各社の売買差(%)を、interval_sec間隔でrounds回サンプリングする。"""
    samples = {name: [] for name in _SOURCES}
    failures = {name: 0 for name in _SOURCES}
    for i in range(rounds):
        started = time.monotonic()
        parts = []
        for name, fetch in _SOURCES.items():
            try:
                bid, ask = fetch()
                if not (bid > 0 and ask > 0) or ask < bid:
                    raise ValueError(f"不正な板 bid={bid} ask={ask}")
                spread = _spread_pct(bid, ask)
                samples[name].append(spread)
                parts.append(f"{name}={spread:.4f}%")
            except Exception as exc:  # 1社の失敗で全体の測定を止めない
                failures[name] += 1
                parts.append(f"{name}=失敗({_describe_error(exc)})")
        logger.info("[SPREAD] %d/%d %s", i + 1, rounds, " ".join(parts))
        if i < rounds - 1:
            time.sleep(max(0.0, interval_sec - (time.monotonic() - started)))
    return samples, failures


def _percentile(sorted_values: list[float], p: float) -> float:
    index = min(len(sorted_values) - 1, int(round(p * (len(sorted_values) - 1))))
    return sorted_values[index]


def summarize(samples: dict, failures: dict, minutes: float, interval_sec: float) -> str:
    lines = [
        "BTC/JPYの売買差(スプレッド)の実測",
        f"Renderのサーバーから{minutes}分間・{int(interval_sec)}秒間隔で測定",
    ]
    for name, values in samples.items():
        if not values:
            lines.append(f"{name}: 測定できず(失敗{failures[name]}回)")
            continue
        ordered = sorted(values)
        median = _percentile(ordered, 0.5)
        p90 = _percentile(ordered, 0.9)
        worst = ordered[-1]
        fee = _TAKER_FEE_PCT[name]
        round_trip = fee * 2 + median
        lines.append(
            f"{name}: 中央値{median:.3f}% / 90%点{p90:.3f}% / 最大{worst:.3f}% "
            f"(有効{len(values)}回・失敗{failures[name]}回)"
        )
        lines.append(f"  → 往復コスト目安 約{round_trip:.3f}% (Taker手数料{fee:.2f}%×2 + 売買差の中央値)")
    lines.append("※往復コスト=成行で買って成行で売る場合。手数料は調査値(2026年10月時点、要確認)")
    lines.append("※売買差は時間帯で変わります。別の時間帯にも測ると確実です")
    return "\n".join(lines)


def run_spread_probe(minutes: int, interval_sec: float = _INTERVAL_SEC) -> str:
    rounds = max(1, int(minutes * 60 / interval_sec))
    samples, failures = collect_samples(rounds, interval_sec)
    summary = summarize(samples, failures, minutes, interval_sec)
    logger.info("[SPREAD] 結果\n%s", summary)
    return summary


def run_spread_probe_in_background(minutes: int) -> threading.Thread:
    """起動を遅らせないよう別スレッドで実行し、終了時に結果をTelegramへ通知する。"""

    def _worker():
        try:
            notify(f"売買差の測定を開始しました({minutes}分間)。結果は終了後に通知します。", level="info")
            notify(run_spread_probe(minutes), level="info")
        except Exception:
            logger.exception("[SPREAD] 測定の実行に失敗しました")

    thread = threading.Thread(target=_worker, daemon=True, name="spread-probe")
    thread.start()
    return thread
