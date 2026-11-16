"""
「レンジ相場ではポジションを持たず、上昇ならロング、下降ならショート」の3状態戦略を、過去データで確かめる検証。

使い方: LONGSHORT_BACKTEST_DAYS に1以上(最大180)を設定して起動すると、起動後に別スレッドで1回だけ
 1. bitbankのBTC/JPYの5分足(直近N日)と、日足(直近8年)を取得
 2. 各時点の相場の状態(+1=ロング、-1=ショート、0=ポジションなし)を、3通りの判定方法で計算
 3. 状態に従う売買を、ロングのみ/ロング+ショート/ショートのみの3通りでシミュレーション
 4. 結果をログ([LONGSHORT])とTelegramに出す
を行う(0なら何もしない。終わったら0に戻すこと)。ライブの取引ロジックには一切手を入れない。

状態の判定方法(事前に決めた3通りだけ。結果を見てから足さない):
 A: ADXが閾値以上のときだけ、移動平均(5/20)の上下でロング/ショート。ADXが閾値未満はポジションなし。
 B: 終値が長期の単純移動平均(5分足=72時間、日足=200日)より上ならロング、下ならショート(常にどちらか)。
 C: Bと同じだが、ADXが閾値未満(レンジ)のときはポジションなし。

売買のルール(固定): 状態が2本連続で同じになったら切り替える。損切りは5%(ショートは逆方向)で、
損切りのあとは、状態が一度変わるまで同じ方向に入り直さない。資金の95%を、レバレッジ1倍で建てる。

コスト: 片道の手数料+売買差(0.05%と0.10%の2通り)。ショートは、持ち越すコスト(建玉手数料)を
0.04%/日として、持っている間ずっと払う。ロングは現物の想定で、持ち越しのコストなし。

近似・限界(結果を読むときの注意):
- 終値で約定、ロスカット(強制決済)は考えない(1倍なので稀だが、急騰時は実際には起こりうる)。
- 持ち越しコストの0.04%/日は、比較サイトの情報で、公式は未確認。
- 5分足は約半年、日足は約8年(2回以上の上げ下げを含む)。5分足の取引数は少ない場合がある。
"""

import logging
import threading
import time
from datetime import datetime, timezone

import config
from improved import momentum_compare as mc
from improved import strategy
from improved.exit_compare import WINDOW_BARS, describe_gaps, fetch_5min_closes, sma_series
from improved.notifier import notify
from improved.spread_probe import _describe_error

logger = logging.getLogger(__name__)

CONFIRM_BARS = 2
STOP_LOSS_PCT = 5.0
SHORT_HOLD_FEE_PCT_PER_DAY = 0.04
COST_PER_SIDE_PCT = (0.05, 0.10)
DAILY_YEARS = 8
KINDS = (("A", "ADX×移動平均5/20"), ("B", "長期平均の上下のみ"), ("C", "長期平均+ADXでレンジ除外"))
POSITION_SETS = (("L", "ロングのみ"), ("LS", "ロング+ショート"), ("S", "ショートのみ"))


# ---------------------------------------------------------------- 状態の判定

def compute_adx_series(prices: list[float], window: int = WINDOW_BARS) -> list:
    adx_values: list = [None] * len(prices)
    for i in range(window - 1, len(prices)):
        adx_values[i] = strategy.calculate_adx(prices[i - window + 1 : i + 1])
        if i % 400 == 0:
            time.sleep(0.01)
    return adx_values


def desired_states(prices, adx_values, sma_short, sma_long, sma_trend, kind: str) -> list[int]:
    thr = config.ADX_THRESHOLD
    out = [0] * len(prices)
    for i, price in enumerate(prices):
        adx = adx_values[i]
        if kind == "A":
            if adx is None or adx < thr or sma_short[i] is None or sma_long[i] is None:
                continue
            out[i] = 1 if sma_short[i] > sma_long[i] else -1 if sma_short[i] < sma_long[i] else 0
        else:
            if sma_trend[i] is None:
                continue
            if kind == "C" and (adx is None or adx < thr):
                continue
            out[i] = 1 if price > sma_trend[i] else -1 if price < sma_trend[i] else 0
    return out


def restrict(states: list[int], position_set: str) -> list[int]:
    if position_set == "LS":
        return list(states)
    if position_set == "L":
        return [s if s > 0 else 0 for s in states]
    return [s if s < 0 else 0 for s in states]


# ---------------------------------------------------------------- シミュレーション

def simulate3(prices, states, bars_per_day: int, cost_side_pct: float,
              hold_fee_day_pct: float = SHORT_HOLD_FEE_PCT_PER_DAY, stop_pct: float = STOP_LOSS_PCT,
              confirm: int = CONFIRM_BARS, ratio: float | None = None) -> dict:
    ratio = config.TRADE_RATIO if ratio is None else ratio
    cost = cost_side_pct / 100
    fee_per_bar = hold_fee_day_pct / 100 / bars_per_day
    equity = 1.0
    pos, entry, notional, fee_acc = 0, None, 0.0, 0.0
    blocked = 0
    prev_state, run = None, 0
    trades: list[tuple[int, float]] = []          # (向き, 手数料込みの損益%(建てた金額に対して))
    peak, max_dd = 1.0, 0.0
    bars_in = {1: 0, -1: 0, 0: 0}

    def close_position(price):
        nonlocal equity, pos, entry, notional, fee_acc
        move = (price - entry) / entry * pos
        equity += notional * move - notional * cost
        trades.append((pos, (move - 2 * cost) * 100 - fee_acc / notional * 100))
        pos, entry, notional, fee_acc = 0, None, 0.0, 0.0

    for price, state in zip(prices, states):
        if pos != 0:
            if pos < 0:
                fee = notional * fee_per_bar
                equity -= fee
                fee_acc += fee
            move = (price - entry) / entry * pos
            if move * 100 <= -stop_pct:
                stopped_direction = pos
                close_position(price)
                blocked = stopped_direction    # 状態が一度変わるまで、同じ方向には入り直さない
        if state == prev_state:
            run += 1
        else:
            prev_state, run = state, 1
        if blocked != 0 and state != blocked:
            blocked = 0
        if state != pos and run >= confirm:
            if pos != 0:
                close_position(price)
            if state != 0 and state != blocked:
                notional = equity * ratio
                equity -= notional * cost
                entry, pos, fee_acc = price, state, 0.0
        bars_in[pos] += 1
        mtm = equity + (notional * (price - entry) / entry * pos if pos != 0 else 0.0)
        peak = max(peak, mtm)
        max_dd = max(max_dd, (peak - mtm) / peak)

    final = equity + (notional * (prices[-1] - entry) / entry * pos - notional * cost if pos != 0 else 0.0)
    wins = [p for _, p in trades if p > 0]
    total_bars = max(1, len(prices))
    return {
        "total_pct": (final - 1) * 100,
        "trades": len(trades),
        "win_pct": len(wins) / len(trades) * 100 if trades else 0.0,
        "avg_pct": sum(p for _, p in trades) / len(trades) if trades else 0.0,
        "max_dd_pct": max_dd * 100,
        "long_sum_pct": sum(p for d, p in trades if d > 0),
        "short_sum_pct": sum(p for d, p in trades if d < 0),
        "long_trades": sum(1 for d, _ in trades if d > 0),
        "short_trades": sum(1 for d, _ in trades if d < 0),
        "time_long_pct": bars_in[1] / total_bars * 100,
        "time_short_pct": bars_in[-1] / total_bars * 100,
        "open_position": pos,
    }


# ---------------------------------------------------------------- レポート

def analyse(label: str, prices: list[float], trend_bars: int, bars_per_day: int, n_splits: int):
    """1つの時間軸について(ログ用の詳細, Telegram用の短い文)を返す。"""
    n = len(prices)
    adx_values = compute_adx_series(prices)
    sma_short = sma_series(prices, config.SHORT_WINDOW)
    sma_long = sma_series(prices, config.LONG_WINDOW)
    sma_trend = sma_series(prices, trend_bars)
    edges = [round(n * k / n_splits) for k in range(n_splits + 1)]
    segments = [slice(edges[k], edges[k + 1]) for k in range(n_splits)]
    bh_all = (prices[-1] / prices[0] - 1) * 100
    bh_seg = [(prices[s][-1] / prices[s][0] - 1) * 100 for s in segments]
    head = f"[{label}] 買い持ち 全{bh_all:+.1f}% 区間別 " + "/".join(f"{x:+.0f}" for x in bh_seg) + "%"
    detail, short = [f"=== {head} ==="], [head]
    for kind, kind_name in KINDS:
        states = desired_states(prices, adx_values, sma_short, sma_long, sma_trend, kind)
        share = {v: states.count(v) / n * 100 for v in (1, -1, 0)}
        info = f"{kind}={kind_name}: 状態の割合 ロング{share[1]:.0f}% ショート{share[-1]:.0f}% なし{share[0]:.0f}%"
        detail.append(info)
        short.append(info)
        for position_set, set_name in POSITION_SETS:
            st = restrict(states, position_set)
            low = simulate3(prices, st, bars_per_day, COST_PER_SIDE_PCT[0])
            high = simulate3(prices, st, bars_per_day, COST_PER_SIDE_PCT[1])
            segs = [simulate3(prices[s], st[s], bars_per_day, COST_PER_SIDE_PCT[0]) for s in segments]
            line = (f" {kind}-{position_set}: {low['total_pct']:+.1f}%({low['trades']}回・{low['win_pct']:.0f}%) "
                    f"区間 " + "/".join(f"{r['total_pct']:+.0f}" for r in segs) + f" | 往復0.2%:{high['total_pct']:+.1f}%")
            short.append(line)
            detail.append(line + f" 平均{low['avg_pct']:+.2f}%/回 DD{low['max_dd_pct']:.0f}% "
                          f"ロング{low['long_trades']}回計{low['long_sum_pct']:+.1f}% "
                          f"ショート{low['short_trades']}回計{low['short_sum_pct']:+.1f}% {set_name}")
    return "\n".join(detail), "\n".join(short)


def fetch_btc_daily(years: int):
    """bitbankのBTC/JPY日足の終値(古い順)と、メモ。"""
    this_year = datetime.now(timezone.utc).year
    rows: dict[int, float] = {}
    notes = []
    for year in range(this_year - years + 1, this_year + 1):
        last_exc = None
        for attempt in range(3):
            try:
                for day, close, _vol in mc.fetch_daily("btc_jpy", year):
                    rows[day] = close
                last_exc = None
                break
            except mc.NoData:
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                time.sleep(1.0 * (attempt + 1))
        if last_exc is not None:
            notes.append(f"{year}年 取得失敗({_describe_error(last_exc)})")
        time.sleep(0.3)
    if len(rows) < 400:
        raise RuntimeError("BTC/JPYの日足が足りません(" + f"{len(rows)}日): " + " / ".join(notes))
    days = sorted(rows)
    return days, [rows[d] for d in days], notes


def run_longshort(days: int):
    started = time.monotonic()
    notify(f"🧪 ロング/ショート/ノーポジションの検証を開始します(5分足{days}日と日足{DAILY_YEARS}年)。結果は終了後に通知します。")
    timestamps, prices5, source, notes5 = fetch_5min_closes(days)
    first = datetime.fromtimestamp(timestamps[0] / 1000, timezone.utc)
    last = datetime.fromtimestamp(timestamps[-1] / 1000, timezone.utc)
    label5 = f"BTC/JPY 5分足 {first:%m/%d}〜{last:%m/%d} UTC {len(prices5):,}本 {describe_gaps(timestamps)}"
    detail5, short5 = analyse(label5, prices5, 72 * 12, 288, 2)

    day_numbers, prices_d, notes_d = fetch_btc_daily(DAILY_YEARS)
    d0 = datetime.fromtimestamp(day_numbers[0] * mc.DAY_MS / 1000, timezone.utc)
    d1 = datetime.fromtimestamp(day_numbers[-1] * mc.DAY_MS / 1000, timezone.utc)
    labeld = f"BTC/JPY 日足 {d0:%Y-%m-%d}〜{d1:%Y-%m-%d} {len(prices_d):,}本"
    detail_d, short_d = analyse(labeld, prices_d, 200, 1, 3)

    text_detail = "\n".join([detail5, detail_d] + notes5 + notes_d)
    for line in text_detail.splitlines():
        logger.info("[LONGSHORT] %s", line)
    legend = ("表記: 全体の損益(取引回数・勝率) 区間別の損益% | 往復0.2%の場合の全体。"
              "L=ロングのみ LS=ロング+ショート S=ショートのみ。手数料は片道0.05%、ショートは0.04%/日")
    short = "\n".join([legend, short5, short_d] + [f"※{n}" for n in notes5 + notes_d]
                      + [f"(所要{time.monotonic() - started:.0f}秒。ロスカットは考慮していません)"])
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
        run_longshort(days)
    except Exception as exc:
        logger.exception("[LONGSHORT] 検証に失敗しました")
        notify(f"ロング/ショート検証に失敗しました: {_describe_error(exc)}", level="error")


def run_longshort_in_background(days: int):
    threading.Thread(target=_safe_run, args=(days,), daemon=True, name="longshort-compare").start()
