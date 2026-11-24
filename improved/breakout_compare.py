"""
USD/JPYのブレイクアウト戦略の検証。GMOコインFXの公開API(1分足のBID/ASK)を、5分足にまとめて使う。

使い方: BREAKOUT_BACKTEST_DAYS に1以上(最大1100)を設定して起動すると、起動後に別スレッドで1回だけ
 1. 直近N日の銀行営業日の1分足(BID・ASK)を取得し、5分足にまとめる(約35分かかる)
 2. 事前に決めた10パターン(下記)をシミュレーション
 3. 結果をログ([BREAKOUT])とTelegramに出す
を行う(0なら何もしない。終わったら0に戻すこと)。ライブの取引ロジックには一切手を入れない。

共通ルール:
- ブレイクは、5分足の中値の高値(安値)が、基準線(+バッファ)を超えた(下回った)ことで判定する。
  中値の高値・安値は、BIDとASKの高値(安値)の平均で近似する。
- エントリーは、ブレイクした5分足の「次の5分足の始値」で、買いはASK、売りはBIDで約定する。
- 1日1回だけ、最初のブレイクの方向に入る。同じ足で上下に抜けたら、その日は見送る。
- 決済は、時刻(その足の始値。買いはBID、売りはASK)。損切りは、5分足のBIDの安値(買い)・ASKの高値(売り)で判定し、
  損切り価格で約定と仮定する(窓開けなら始値)。
- コスト: スプレッドは、BID/ASKの約定で自動的に入る。API手数料は、往復0.4pips(1通貨あたり0.002円/約定、
  2025年3月版の説明書。最新は未確認)。

パターン(結果を見る前に固定。後から足さない):
 A. 東京ボックス: 9:00〜15:00(日本時間)の高値・安値を基準に、15:00以降(最終は22:55の足)のブレイクに乗り、
    23:00に決済。バッファ 0/2pips × 損切り なし/ボックスの反対側 = 4通り。
 B. オープニングレンジ: 9:00〜9:30の高値・安値を基準に、9:30以降(最終は10:55の足)のブレイクに乗り、
    11:00または15:00に決済。損切り なし/レンジの反対側。全営業日と「ゴトー日を除く日」の両方 = 4通り×2グループ。
 C. 前営業日の高値・安値(6:00〜翌5:59)のブレイク: 9:00〜15:00(最終は14:55の足)で入り、15:00または23:00に決済。損切りなし = 2通り。

「候補」とみなす条件(4項目すべて。パターンが多いので厳しくしてある):
 1. t値が3以上  2. 前半・後半(取引の古い順に2分割)とも平均がプラス
 3. 片道0.5pips悪化でも平均がプラス  4. 方向の並べ替え検定でp<0.01
 (方向の並べ替え検定: 各取引の方向をランダムに入れ替えた場合の平均と比べ、ブレイクの方向に乗ることに価値があるかを見る。
  損切りで決済した取引も、同じ決済価格で入れ替える近似。)

近似・限界: 欧州の夏時間で、ロンドンの開場は日本時間の16時台と17時台で動くが、時刻は固定。データは約3年(円安局面)で、
 5分足の近似。結果を、そのまま信じない。
"""

import logging
import random
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests

from improved.jp_calendar import gotobi_days, is_business_day
from improved.nakane_compare import (API_FEE_PIPS_PER_FILL, DATA_START, FETCH_SLEEP_SEC, JST, MAX_FAILURE_RATE,
                                     PIP, PUBLIC_API, SYMBOL, bootstrap_ci, hm, stats)
from improved.notifier import notify
from improved.spread_probe import _describe_error

logger = logging.getLogger(__name__)

M = lambda h, m: h * 60 + m
BAR = 5                                  # 5分足
PERMUTATION_ROUNDS = 5000
BOOTSTRAP_ROUNDS = 3000
MIN_BOX_BARS_RATIO = 0.6                 # 基準の区間で、足が6割以上そろっている日だけ使う


# ---------------------------------------------------------------- データ取得(1分足 → 5分足)

def aggregate_5min(rows, day: date):
    """1分足の行から、{5分の開始分: (始値, 高値, 安値)}(当日の日本時間分のみ)と、ファイル全体の(高値, 安値)を返す。"""
    buckets: dict[int, list] = {}
    full_hi, full_lo = None, None
    for row in sorted(rows, key=lambda r: int(r["openTime"])):
        t = datetime.fromtimestamp(int(row["openTime"]) / 1000, JST)
        o, h, l = float(row["open"]), float(row["high"]), float(row["low"])
        full_hi = h if full_hi is None else max(full_hi, h)
        full_lo = l if full_lo is None else min(full_lo, l)
        if t.date() != day:
            continue
        start = (t.hour * 60 + t.minute) // BAR * BAR
        b = buckets.get(start)
        if b is None:
            buckets[start] = [o, h, l]
        else:
            b[1] = max(b[1], h)
            b[2] = min(b[2], l)
    return {k: tuple(v) for k, v in buckets.items()}, full_hi, full_lo


def fetch_day_5min(day: date, price_type: str):
    resp = requests.get(PUBLIC_API, params={"symbol": SYMBOL, "priceType": price_type, "interval": "1min",
                                            "date": f"{day:%Y%m%d}"}, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != 0:
        raise ValueError(f"GMOコインAPIがエラーを返しました: {payload.get('messages')}")
    return aggregate_5min(payload.get("data", []), day)


def fetch_all(days: int, sleep_sec: float = FETCH_SLEEP_SEC):
    """{日: {"BID": {...}, "ASK": {...}, "hi": 前日比較用の全体高値(中値近似), "lo": ...}} とメモ。銀行営業日のみ。"""
    today = datetime.now(JST).date()
    end = today - timedelta(days=1)
    start = max(DATA_START, end - timedelta(days=days - 1))
    targets = [d for d in (start + timedelta(days=i) for i in range((end - start).days + 1)) if is_business_day(d)]
    data, failures, empty = {}, [], 0
    for i, day in enumerate(targets):
        if len(failures) > max(5, len(targets) * MAX_FAILURE_RATE):
            raise RuntimeError(f"取得失敗が多すぎます({len(failures)}/{len(targets)}日): " + " / ".join(failures[:3]))
        got = {}
        for side in ("BID", "ASK"):
            last_exc = None
            for attempt in range(3):
                try:
                    got[side] = fetch_day_5min(day, side)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    time.sleep(1.0 * (attempt + 1))
            if last_exc is not None:
                failures.append(f"{day} {side}: {_describe_error(last_exc)}")
                break
            time.sleep(sleep_sec)
        else:
            (bid, bhi, blo), (ask, ahi, alo) = got["BID"], got["ASK"]
            if bid and ask and bhi is not None and ahi is not None:
                data[day] = {"BID": bid, "ASK": ask, "hi": (bhi + ahi) / 2, "lo": (blo + alo) / 2}
            else:
                empty += 1
        if (i + 1) % 100 == 0:
            logger.info("[BREAKOUT] 取得 %d/%d日", i + 1, len(targets))
    if not data:
        raise RuntimeError("1分足が1日分も取得できませんでした: " + " / ".join(failures[:3]))
    notes = [f"対象の営業日{len(targets)}日、取得{len(data)}日、データ無し{empty}日、失敗{len(failures)}件"] + failures[:5]
    return data, notes, start, end


# ---------------------------------------------------------------- 売買のシミュレーション

def mid_bar(day_data, start_min: int):
    """5分足の(中値の始値, 中値の高値, 中値の安値)。BIDとASKの平均で近似。足が欠けていればNone。"""
    b, a = day_data["BID"].get(start_min), day_data["ASK"].get(start_min)
    if b is None or a is None:
        return None
    return (b[0] + a[0]) / 2, (b[1] + a[1]) / 2, (b[2] + a[2]) / 2


def box_levels(day_data, start_min: int, end_min: int):
    """start_min以上end_min未満の5分足の、中値の高値の最大・安値の最小。足が6割未満ならNone。"""
    highs, lows = [], []
    slots = range(start_min, end_min, BAR)
    for m in slots:
        bar = mid_bar(day_data, m)
        if bar is not None:
            highs.append(bar[1])
            lows.append(bar[2])
    if len(highs) < len(slots) * MIN_BOX_BARS_RATIO:
        return None
    return max(highs), min(lows)


def breakout_trade(day_data, levels, watch_start: int, watch_last: int, buffer_pips: float, exit_min: int,
                   use_stop: bool):
    """最初のブレイクの次の足の始値で入り、exit_minの足の始値で決済する。
    戻り値: dict(direction, gross, net, stopped, mid_move, cost) または None(ブレイクなし・同時ブレイク・約定不可)。"""
    hi, lo = levels
    for m in range(watch_start, watch_last + BAR, BAR):
        bar = mid_bar(day_data, m)
        if bar is None:
            continue
        up = bar[1] > hi + buffer_pips * PIP
        down = bar[2] < lo - buffer_pips * PIP
        if up and down:
            return None
        if not (up or down):
            continue
        direction = 1 if up else -1
        entry_min = m + BAR
        if entry_min >= exit_min:
            return None
        b_in, a_in = day_data["BID"].get(entry_min), day_data["ASK"].get(entry_min)
        b_out, a_out = day_data["BID"].get(exit_min), day_data["ASK"].get(exit_min)
        if b_in is None or a_in is None or b_out is None or a_out is None:
            return None
        entry = a_in[0] if direction > 0 else b_in[0]
        entry_mid = (a_in[0] + b_in[0]) / 2
        stop_level = (lo if direction > 0 else hi) if use_stop else None
        exit_price, exit_mid, stopped = (b_out[0] if direction > 0 else a_out[0]), (a_out[0] + b_out[0]) / 2, False
        if stop_level is not None:
            for k in range(entry_min, exit_min, BAR):
                bid, ask = day_data["BID"].get(k), day_data["ASK"].get(k)
                if bid is None or ask is None:
                    continue
                if direction > 0 and bid[2] <= stop_level:
                    exit_price = min(stop_level, bid[0]) if bid[0] <= stop_level else stop_level
                    exit_mid, stopped = exit_price + (ask[0] - bid[0]) / 2, True
                    break
                if direction < 0 and ask[1] >= stop_level:
                    exit_price = max(stop_level, ask[0]) if ask[0] >= stop_level else stop_level
                    exit_mid, stopped = exit_price - (ask[0] - bid[0]) / 2, True
                    break
        gross = direction * (exit_price - entry) / PIP
        net = gross - 2 * API_FEE_PIPS_PER_FILL
        mid_move = (exit_mid - entry_mid) / PIP
        return {"direction": direction, "gross": gross, "net": net, "stopped": stopped,
                "mid_move": mid_move, "cost": direction * mid_move - net}
    return None


def prev_day_levels(data, day: date, sorted_days: list[date], index: dict):
    i = index[day]
    if i == 0:
        return None
    prev = sorted_days[i - 1]
    if (day - prev).days > 5:
        return None
    return data[prev]["hi"], data[prev]["lo"]


def run_pattern(data, sorted_days, index, gotobi, spec, group: str):
    """spec: dict(kind, ...)。groupは"ALL"または"NOG"(ゴトー日を除く)。"""
    trades = []
    for day in sorted_days:
        if group == "NOG" and day in gotobi:
            continue
        d = data[day]
        if spec["kind"] == "C":
            levels = prev_day_levels(data, day, sorted_days, index)
        else:
            levels = box_levels(d, spec["box_start"], spec["box_end"])
        if levels is None:
            continue
        res = breakout_trade(d, levels, spec["watch_start"], spec["watch_last"], spec.get("buffer", 0.0),
                             spec["exit"], spec.get("stop", False))
        if res is not None:
            res["day"] = day
            trades.append(res)
    return trades


def patterns():
    out = []
    for buffer in (0.0, 2.0):
        for stop in (False, True):
            out.append((f"A 東京ボックス buf{buffer:g} {'損切りあり' if stop else '損切りなし'}",
                        dict(kind="A", box_start=M(9, 0), box_end=M(15, 0), watch_start=M(15, 0), watch_last=M(22, 55),
                             buffer=buffer, exit=M(23, 0), stop=stop), ("ALL",)))
    for exit_min in (M(11, 0), M(15, 0)):
        for stop in (False, True):
            out.append((f"B ORB 決済{hm(exit_min)} {'損切りあり' if stop else '損切りなし'}",
                        dict(kind="B", box_start=M(9, 0), box_end=M(9, 30), watch_start=M(9, 30), watch_last=M(10, 55),
                             exit=exit_min, stop=stop), ("ALL", "NOG")))
    for exit_min in (M(15, 0), M(23, 0)):
        out.append((f"C 前日高安 決済{hm(exit_min)}",
                    dict(kind="C", watch_start=M(9, 0), watch_last=M(14, 55), exit=exit_min), ("ALL",)))
    return out


# ---------------------------------------------------------------- 評価

def direction_permutation_p(trades, rounds: int = PERMUTATION_ROUNDS, seed: int = 2468) -> float:
    """各取引の方向をランダムに入れ替えたときの平均が、実際の平均以上になる確率(片側)。"""
    rng = random.Random(seed)
    n = len(trades)
    actual = sum(t["net"] for t in trades) / n
    moves = [t["mid_move"] for t in trades]
    costs = [t["cost"] for t in trades]
    hits = 0
    for _ in range(rounds):
        total = 0.0
        for m, c in zip(moves, costs):
            total += (m if rng.random() < 0.5 else -m) - c
        hits += total / n >= actual
    return (hits + 1) / (rounds + 1)


def evaluate(trades):
    nets = [t["net"] for t in trades]
    st = stats(nets)
    out = {"n": st["n"], "stats": st}
    if st["n"] < 10:
        out.update(ok=False, reasons=["取引数が少ない(10回未満)"])
        return out
    lo, hi = bootstrap_ci(nets, BOOTSTRAP_ROUNDS)
    half = len(trades) // 2
    first, second = stats(nets[:half]), stats(nets[half:])
    worse = stats([x - 1.0 for x in nets])["mean"]            # 片道0.5pips悪化 = 往復1.0pips
    p = direction_permutation_p(trades)
    checks = [("t≥3", st["t"] >= 3), ("前半後半ともプラス", first["mean"] > 0 and second["mean"] > 0),
              ("0.5pips悪化でもプラス", worse > 0), ("方向検定p<0.01", p < 0.01)]
    out.update(ci=(lo, hi), first=first, second=second, worse=worse, p=p, checks=checks,
               ok=all(ok for _, ok in checks), reasons=[name for name, ok in checks if not ok],
               stop_rate=sum(1 for t in trades if t["stopped"]) / len(trades) * 100,
               long_mean=stats([t["net"] for t in trades if t["direction"] > 0])["mean"],
               short_mean=stats([t["net"] for t in trades if t["direction"] < 0])["mean"],
               long_n=sum(1 for t in trades if t["direction"] > 0),
               by_year={y: stats([t["net"] for t in trades if t["day"].year == y])["mean"]
                        for y in sorted({t["day"].year for t in trades})})
    return out


def build_reports(data, header: str):
    sorted_days = sorted(data)
    index = {d: i for i, d in enumerate(sorted_days)}
    gotobi = gotobi_days(sorted_days[0], sorted_days[-1])
    detail = [header, f"営業日{len(sorted_days)}日(ゴトー日{sum(1 for d in sorted_days if d in gotobi)})"]
    short = [header, "表記: 平均pips(勝率,t値,n) 前半/後半 | 0.5pips悪化 | 方向検定p。API手数料込み"]
    candidates = []
    for name, spec, groups in patterns():
        for group in groups:
            trades = run_pattern(data, sorted_days, index, gotobi, spec, group)
            label = name + ("" if group == "ALL" else "(ゴトー日除く)")
            ev = evaluate(trades)
            st = ev["stats"]
            if "checks" not in ev:
                line = f"{label}: n{st['n']} {ev['reasons'][0]}"
                detail.append(line)
                short.append(line)
                continue
            line = (f"{label}: {st['mean']:+.1f}({st['win']:.0f}%,t{st['t']:+.1f},n{st['n']}) "
                    f"{ev['first']['mean']:+.1f}/{ev['second']['mean']:+.1f} | {ev['worse']:+.1f} | p{ev['p']:.3f}")
            short.append(line)
            detail.append(line + f" CI[{ev['ci'][0]:+.1f},{ev['ci'][1]:+.1f}] 買い{ev['long_n']}回{ev['long_mean']:+.1f}/"
                                 f"売り{st['n'] - ev['long_n']}回{ev['short_mean']:+.1f} "
                                 f"損切り{ev['stop_rate']:.0f}% 年別" + " ".join(f"{y}:{m:+.1f}" for y, m in ev["by_year"].items())
                          + (" 未達:" + ",".join(ev["reasons"]) if ev["reasons"] else " 全項目達成"))
            if ev["ok"]:
                candidates.append(label)
    short += ["", f"■事前に決めた4条件をすべて満たした候補: " + (", ".join(candidates) if candidates else "なし"),
              "※10パターンを見た結果。達成しても偶然の可能性は残る。3年・円安局面に偏る。5分足の近似。"]
    detail.append("候補: " + (", ".join(candidates) if candidates else "なし"))
    return "\n".join(detail), "\n".join(short)


def run_breakout(days: int):
    started = time.monotonic()
    notify(f"🧪 USD/JPYブレイクアウトの検証を開始します(直近{days}日・5分足)。データ取得に約35分かかります。")
    data, notes, start, end = fetch_all(days)
    for note in notes:
        logger.info("[BREAKOUT] %s", note)
    header = f"USD/JPY 5分足(GMOコインFX) {start}〜{end} 取得{len(data)}日"
    detail, short = build_reports(data, header)
    for line in detail.splitlines():
        logger.info("[BREAKOUT] %s", line)
    if len(notes) > 1:
        short += "\n※取得の問題: " + " / ".join(notes[1:])[:300]
    short += f"\n(所要{time.monotonic() - started:.0f}秒)"
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
        run_breakout(days)
    except Exception as exc:
        logger.exception("[BREAKOUT] 検証に失敗しました")
        notify(f"ブレイクアウトの検証に失敗しました: {_describe_error(exc)}", level="error")


def run_breakout_in_background(days: int):
    threading.Thread(target=_safe_run, args=(days,), daemon=True, name="breakout-compare").start()
