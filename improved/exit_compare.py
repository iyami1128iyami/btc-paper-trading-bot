"""
損切り幅と「負けトレードの扱い」を比べるバックテスト(案A)。

現在の決済ルール(損切り-5%固定、含み損のときは売りシグナルを無視)が、損益の
非対称(負けが大きく勝ちが小さい)を生んでいないかを、過去データで比べて確かめる。

使い方: BACKTEST_COMPARE_DAYS に1以上(最大180)を設定して起動すると、起動後に別スレッドで
 1. 国内取引所(bitbank、だめならGMOコイン)の公開APIから、BTC/JPYの5分足終値を取得
 2. 現在の戦略(improved.strategy.generate_signal)で、各時点のシグナルを1回だけ計算
 3. 決済ルールだけを変えた複数のパターンを、同じシグナルでシミュレーション
 4. 結果をログ([COMPARE])とTelegramに出す
を行う(0なら何もしない。終わったら0に戻すこと)。

ライブの取引ロジック(paper_trader / demo_trader / backtest_engine)には一切手を入れない。
このモジュールは比較専用の簡易シミュレーターを持つ。

近似・限界(結果を読むときの注意):
- ライブはCoinGeckoのUSD建て(約5分間隔)の価格、ここは取引所のBTC/JPYの5分足終値。
  戦略は%ベースなので傾向は近いが、一致はしない。
- ライブは毎分、最新価格で決済条件を見るが、ここは5分足の終値だけで見る(約定の細かさが違う)。
- 約定は終値、手数料は片道の固定率(スリッページは売買差込みの粗い見積もり)。
- 数十日・1つの相場局面・取引数も数十回なので、統計的な確かさは低い。傾向をつかむ目的に限る。
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

import config
from improved import strategy
from improved.notifier import notify
from improved.spread_probe import _describe_error

logger = logging.getLogger(__name__)

WINDOW_BARS = 100           # ライブの fetch_klines(limit=100) と同じ窓の長さ
BAR_SECONDS = 300           # 5分足
TAKE_PROFIT_PCT = 10.0      # ライブ既定値
MIN_BARS_PER_DAY = 200      # 1日分(288本)として最低限これだけあれば有効とみなす
COST_PER_SIDE_PCT = (0.0, 0.05, 0.10)   # 片道の手数料+売買差(%)。往復で 0 / 0.10 / 0.20
STOP_LOSS_CHOICES = (5.0, 3.0, 2.0, 1.5)


# ---------------------------------------------------------------- データ取得

def _get_json(url, params=None):
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _fetch_bitbank_day(day) -> list[tuple[int, float]]:
    payload = _get_json(f"https://public.bitbank.cc/btc_jpy/candlestick/5min/{day:%Y%m%d}")
    if payload.get("success") != 1:
        raise ValueError(f"bitbank APIがエラーを返しました: {payload.get('data')}")
    candles = payload["data"]["candlestick"]
    if not candles:
        return []
    # ohlcv: [open, high, low, close, volume, timestamp_ms](値は文字列)
    return [(int(row[5]), float(row[3])) for row in candles[0]["ohlcv"]]


def _fetch_gmo_day(day) -> list[tuple[int, float]]:
    payload = _get_json(
        "https://api.coin.z.com/public/v1/klines",
        {"symbol": "BTC", "interval": "5min", "date": f"{day:%Y%m%d}"},
    )
    if payload.get("status") != 0:
        raise ValueError(f"GMOコインAPIがエラーを返しました: {payload.get('messages')}")
    return [(int(row["openTime"]), float(row["close"])) for row in payload["data"]]


_SOURCES = (("bitbank", _fetch_bitbank_day), ("GMOコイン", _fetch_gmo_day))


def fetch_5min_closes(days: int, sleep_sec: float = 0.4):
    """
    直近days日ぶんの5分足終値を古い順に返す: (timestamps_ms, closes, source_name, notes)。
    1日ずつ取得する。ある日の取得に失敗したら、それより古い分は捨て、最新側の連続した区間だけを使う。
    """
    today = datetime.now(timezone.utc).date()
    day_list = [today - timedelta(days=d) for d in range(days - 1, -1, -1)]
    probe_day = day_list[-2] if len(day_list) >= 2 else day_list[-1]

    chosen, errors = None, []
    for name, fn in _SOURCES:
        try:
            rows = fn(probe_day)
            if len(rows) < MIN_BARS_PER_DAY:
                raise ValueError(f"1日分のデータが少なすぎます({len(rows)}本)")
            chosen = (name, fn)
            break
        except Exception as exc:
            errors.append(f"{name}: {_describe_error(exc)}")
            logger.warning("[COMPARE] %s から5分足を取得できません: %s", name, _describe_error(exc))
    if chosen is None:
        raise RuntimeError("5分足を取得できる取引所がありません: " + " / ".join(errors))

    source_name, fetch_day = chosen
    collected: dict[int, float] = {}
    notes = []
    for idx, day in enumerate(day_list):
        last_exc = None
        for attempt in range(3):
            try:
                for ts, close in fetch_day(day):
                    if close > 0:
                        collected[ts] = close
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                time.sleep(1.0 * (attempt + 1))
        if last_exc is not None:
            notes.append(f"{day} 取得失敗({_describe_error(last_exc)})")
            if idx != len(day_list) - 1:   # 最終日(今日)の失敗は無視。それ以外は古い側を捨てる
                collected.clear()
        time.sleep(sleep_sec)

    if not collected:
        raise RuntimeError("5分足のデータが1本も取得できませんでした: " + " / ".join(notes or errors))
    ordered = sorted(collected.items())
    return [t for t, _ in ordered], [c for _, c in ordered], source_name, notes


def describe_gaps(timestamps_ms: list[int]) -> str:
    gaps = [(b - a) / 1000 for a, b in zip(timestamps_ms, timestamps_ms[1:])]
    big = [g for g in gaps if g > BAR_SECONDS * 3]
    if not big:
        return "欠測なし"
    return f"15分超の欠測 {len(big)}か所(最大{max(big) / 3600:.1f}時間)"


# ---------------------------------------------------------------- シグナル

def compute_signals(prices: list[float], window: int = WINDOW_BARS) -> list[str]:
    """各時点で「その時点までの直近window本」だけから戦略のシグナルを計算する(ライブと同じ形)。"""
    signals = ["HOLD"] * len(prices)
    strategy_logger = logging.getLogger(strategy.__name__)
    previous_level = strategy_logger.level
    strategy_logger.setLevel(logging.WARNING)   # シグナルごとのINFOログで本番ログを埋めない
    try:
        for i in range(window - 1, len(prices)):
            signals[i] = strategy.generate_signal(prices[i - window + 1 : i + 1])
            if i % 400 == 0:
                time.sleep(0.01)   # 取引ループにCPUを譲る(無料プランは非力なため)
    finally:
        strategy_logger.setLevel(previous_level)
    return signals


# ---------------------------------------------------------------- シミュレーション

@dataclass(frozen=True)
class Variant:
    name: str
    stop_loss_pct: float
    loss_exit_on_signal: bool   # Trueなら、含み損のときも売りシグナルで決済する


@dataclass
class SimResult:
    trades: int
    win_rate_pct: float
    avg_pnl_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    return_pct: float
    max_dd_pct: float
    open_position: bool


def simulate(prices, signals, variant: Variant, cost_per_side_pct: float = 0.0) -> SimResult:
    """
    決済ルールだけを変えたシミュレーション。ベースライン(損切り5%・loss_exit_on_signal=False・
    手数料0)は improved.backtest_engine.BacktestTrader と同じ結果になる(テストで確認)。
    """
    cost = cost_per_side_pct / 100
    ratio = config.TRADE_RATIO
    cash, qty = 1.0, 0.0
    entry = highest = spent = None
    pnls: list[float] = []
    peak, max_dd = 1.0, 0.0

    for price, signal in zip(prices, signals):
        if qty > 0:
            highest = max(highest, price)
            change = (price - entry) / entry * 100
            exit_now = False
            if change >= TAKE_PROFIT_PCT or change <= -variant.stop_loss_pct:
                exit_now = True
            else:
                peak_change = (highest - entry) / entry * 100
                if peak_change >= config.TRAILING_STOP_ACTIVATION_PCT:
                    if (highest - price) / highest * 100 >= config.TRAILING_STOP_PCT:
                        exit_now = True
            if not exit_now and signal == "SELL":
                if change >= config.MIN_PROFIT_PCT_TO_EXIT or (variant.loss_exit_on_signal and change < 0):
                    exit_now = True
            if exit_now:
                proceeds = qty * price * (1 - cost)
                pnls.append((proceeds - spent) / spent * 100)
                cash += proceeds
                qty, entry, highest, spent = 0.0, None, None, None
        elif signal == "BUY":
            spent = cash * ratio
            qty = spent * (1 - cost) / price
            cash -= spent
            entry = highest = price

        value = cash + qty * price
        peak = max(peak, value)
        max_dd = max(max_dd, (peak - value) / peak * 100)

    final_value = cash + qty * prices[-1]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return SimResult(
        trades=len(pnls),
        win_rate_pct=len(wins) / len(pnls) * 100 if pnls else 0.0,
        avg_pnl_pct=sum(pnls) / len(pnls) if pnls else 0.0,
        avg_win_pct=sum(wins) / len(wins) if wins else 0.0,
        avg_loss_pct=sum(losses) / len(losses) if losses else 0.0,
        return_pct=(final_value - 1.0) * 100,
        max_dd_pct=max_dd,
        open_position=qty > 0,
    )


def build_variants() -> list[Variant]:
    variants = []
    for loss_exit in (False, True):
        for sl in STOP_LOSS_CHOICES:
            label = f"SL{sl:g}" + ("+負けも売" if loss_exit else "")
            variants.append(Variant(label, sl, loss_exit))
    return variants


# ---------------------------------------------------------------- 実行・レポート

def _short(result: SimResult) -> str:
    return f"{result.return_pct:+.1f}%({result.trades}回{result.win_rate_pct:.0f}%)"


def build_reports(prices, signals, header: str):
    """(ログ用の詳細テキスト, Telegram用の短いテキスト) を返す。"""
    mid = len(prices) // 2
    halves = {"前半": slice(0, mid), "後半": slice(mid, len(prices))}
    bh = {name: (prices[s][-1] / prices[s][0] - 1) * 100 for name, s in halves.items()}
    bh_all = (prices[-1] / prices[0] - 1) * 100
    variants = build_variants()

    detail = [header, f"買い持ち: 前半{bh['前半']:+.1f}% 後半{bh['後半']:+.1f}% 全体{bh_all:+.1f}%"]
    short = [header, f"買い持ち 前{bh['前半']:+.1f}% 後{bh['後半']:+.1f}% 全{bh_all:+.1f}%",
             "表記: 損益(取引回数 勝率) / 損切り幅SL、売=含み損でも売りシグナルで決済"]
    for cost_side in COST_PER_SIDE_PCT:
        detail.append(f"--- 往復コスト {cost_side * 2:.2f}% ---")
        if cost_side in (0.05, 0.10):
            short.append(f"■往復コスト{cost_side * 2:.2f}%")
        for v in variants:
            res_half = {n: simulate(prices[s], signals[s], v, cost_side) for n, s in halves.items()}
            res_all = simulate(prices, signals, v, cost_side)
            detail.append(
                f"{v.name:<10} 全体{_short(res_all)} 平均{res_all.avg_pnl_pct:+.2f}% "
                f"勝{res_all.avg_win_pct:+.2f}%/負{res_all.avg_loss_pct:+.2f}% DD{res_all.max_dd_pct:.1f}% | "
                f"前半{_short(res_half['前半'])} 後半{_short(res_half['後半'])}"
            )
            if cost_side in (0.05, 0.10):
                short.append(f"{v.name}: 前{_short(res_half['前半'])} 後{_short(res_half['後半'])}")
    return "\n".join(detail), "\n".join(short)


def run_comparison(days: int):
    started = time.monotonic()
    notify(f"🧪 決済ルールの比較バックテストを開始します(直近{days}日・5分足)。結果は終了後に通知します。")
    timestamps, prices, source, notes = fetch_5min_closes(days)
    first = datetime.fromtimestamp(timestamps[0] / 1000, timezone.utc)
    last = datetime.fromtimestamp(timestamps[-1] / 1000, timezone.utc)
    logger.info("[COMPARE] %s の5分足 %d本を取得(%s〜%s UTC) %s", source, len(prices), first, last, describe_gaps(timestamps))
    for note in notes:
        logger.warning("[COMPARE] %s", note)

    if len(prices) < WINDOW_BARS + 300:
        raise RuntimeError(f"データが少なすぎます({len(prices)}本)")
    signals = compute_signals(prices)
    buys = signals.count("BUY")
    sells = signals.count("SELL")
    logger.info("[COMPARE] シグナル数: BUY %d / SELL %d", buys, sells)

    header = (
        f"BTC/JPY 5分足 {first:%m/%d}〜{last:%m/%d} UTC({len(prices):,}本, {source}) "
        f"{describe_gaps(timestamps)} / BUY{buys}・SELL{sells}"
    )
    detail, short = build_reports(prices, signals, header)
    for line in detail.splitlines():
        logger.info("[COMPARE] %s", line)
    if notes:
        short += "\n※取得失敗あり: " + " / ".join(notes)[:300]
    elapsed = time.monotonic() - started
    short += f"\n(所要{elapsed:.0f}秒。5分足の近似で、取引数が少ないため傾向の確認用です)"
    # Telegramの1通あたりの長さ制限に収まるよう、必要なら分割して送る
    chunk, chunks = [], []
    for line in short.splitlines():
        if sum(len(x) + 1 for x in chunk) + len(line) > 1500:
            chunks.append("\n".join(chunk))
            chunk = []
        chunk.append(line)
    chunks.append("\n".join(chunk))
    for text in chunks:
        notify(text)
        time.sleep(1.0)


def _safe_run(days: int):
    try:
        run_comparison(days)
    except Exception as exc:
        logger.exception("[COMPARE] 比較バックテストに失敗しました")
        notify(f"比較バックテストに失敗しました: {_describe_error(exc)}", level="error")


def run_comparison_in_background(days: int):
    threading.Thread(target=_safe_run, args=(days,), daemon=True, name="exit-compare").start()
