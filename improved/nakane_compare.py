"""
仲値トレード(ドル円)の検証。GMOコイン 外国為替FXの公開API(1分足のBID/ASK、認証不要)で、
「ゴトー日の朝にドルを買い、仲値の直前に決済する」といった手法に優位があるかを、過去データで確かめる。

使い方: NAKANE_BACKTEST_DAYS に1以上(最大1100)を設定して起動すると、起動後に別スレッドで1回だけ
 1. 直近N日の銀行営業日について、USD/JPYの1分足(BID・ASK)を取得(1リクエスト/秒。約25〜40分かかる)
 2. 日を「ゴトー日(G)」「ゴトー日でない金曜(F)」「それ以外の営業日(N)」に分け、時間帯ごとの値動きを集計
 3. 事前に決めたルールで、実際の売買(買いはASK、売りはBIDで約定)をシミュレーション
 4. 結果をログ([NAKANE])とTelegramに出す
を行う(0なら何もしない。終わったら0に戻すこと)。ライブの取引ロジックには一切手を入れない。

検証するルール(事前に決めたものだけ。結果を見てから足さない):
 [A] 時間帯の値動き(中値、手数料前): 8:00→9:54 / 9:00→9:54 / 9:54→9:57 / 9:55→10:30 / 9:55→11:00 を
     G・F・Nで比べる。ゴトー日が特別かどうかは、Nとの差で見る。
 [B] ロング: 8:00または9:00に成行で買い、9:54に決済。損切り なし/-20pips/-30pips。
     グループ: G、G+F(ゴトー日と金曜)、全営業日(参考)。押し目フィルター(下記)付きはGのみ。
 [C] ショート(ドテン売り): 9:55に売り、10:30または11:00に決済。損切り なし/-30pips。
 押し目フィルター: エントリー時の中値が、当日6:00(日本時間)の中値より低いときだけ買う
     (「前日終値より安い」の私の解釈。6:00は、NYクローズ直後の価格)。

コスト: 買いはASKの始値、売りはBIDの始値で約定するので、スプレッドは自動で入る。API手数料は、
 1通貨あたり0.002円/約定(2025年3月版の説明書。最新は未確認)=0.2pips×2(往復0.4pips)を引いた値と、
 引かない値の両方を出す。スワップはなし(8時〜11時に、ロールオーバーをまたがない)。

近似・限界(結果を読むときの注意):
- 約定は1分足の始値で、スリッページは考えない。損切りは1分足の安値/高値で判定し、損切り価格で約定と仮定。
- データは2023年10月28日以降(約3年)。ゴトー日は約200回で、1つの相場局面(円安・介入)に偏る。
- 組み合わせが多いので、見かけ上の「有意」は、偶然で出やすい(多重比較)。t値が2を超えても、そのまま信じない。
"""

import logging
import statistics
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests

from improved.jp_calendar import gotobi_days, is_business_day
from improved.notifier import notify
from improved.spread_probe import _describe_error

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
PUBLIC_API = "https://forex-api.coin.z.com/public/v1/klines"
DATA_START = date(2023, 10, 28)       # 1分足の取得が可能な最初の日付(公式ドキュメント)
SYMBOL = "USD_JPY"
FETCH_SLEEP_SEC = 1.0
MAX_FAILURE_RATE = 0.10
PIP = 0.01                             # 円ペアの1pips=0.01円
API_FEE_PIPS_PER_FILL = 0.2            # 0.002円/通貨 = 0.2pips(約定ごと)
MIN_MINUTE, MAX_MINUTE = 6 * 60, 11 * 60 + 5     # 保存する時間帯 6:00〜11:05(日本時間)
ENTRY_TOLERANCE_MIN = 3                # 指定の分に足がなければ、最大3分後の足まで許す

M = lambda h, m: h * 60 + m
WINDOWS = ((M(8, 0), M(9, 54)), (M(9, 0), M(9, 54)), (M(9, 54), M(9, 57)), (M(9, 55), M(10, 30)), (M(9, 55), M(11, 0)))
STOPS_LONG = (None, 20, 30)
STOPS_SHORT = (None, 30)
GROUPS = (("G", "ゴトー日"), ("GF", "ゴトー日+金曜"), ("ALL", "全営業日"))


def hm(minute: int) -> str:
    return f"{minute // 60}:{minute % 60:02d}"


# ---------------------------------------------------------------- データ取得

def fetch_day(day: date, price_type: str) -> dict[int, tuple]:
    """その日(6:00〜翌5:59 JST)のBID/ASK 1分足から、6:00〜11:05の分だけを {分: (始, 高, 安, 終)} で返す。"""
    resp = requests.get(PUBLIC_API, params={"symbol": SYMBOL, "priceType": price_type, "interval": "1min",
                                            "date": f"{day:%Y%m%d}"}, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != 0:
        raise ValueError(f"GMOコインAPIがエラーを返しました: {payload.get('messages')}")
    out: dict[int, tuple] = {}
    for row in payload.get("data", []):
        t = datetime.fromtimestamp(int(row["openTime"]) / 1000, JST)
        if t.date() != day:
            continue
        minute = t.hour * 60 + t.minute
        if MIN_MINUTE <= minute <= MAX_MINUTE:
            out[minute] = (float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]))
    return out


def fetch_all(days: int, sleep_sec: float = FETCH_SLEEP_SEC):
    """{日: {"BID": {...}, "ASK": {...}}} と、メモ。銀行営業日だけを取得する。"""
    today = datetime.now(JST).date()
    end = today - timedelta(days=1)
    start = max(DATA_START, end - timedelta(days=days - 1))
    targets = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    targets = [d for d in targets if is_business_day(d)]
    data: dict[date, dict[str, dict]] = {}
    failures, empty = [], 0
    for i, day in enumerate(targets):
        if len(failures) > max(5, len(targets) * MAX_FAILURE_RATE):
            raise RuntimeError(f"取得失敗が多すぎます({len(failures)}/{len(targets)}日): " + " / ".join(failures[:3]))
        got = {}
        for side in ("BID", "ASK"):
            last_exc = None
            for attempt in range(3):
                try:
                    got[side] = fetch_day(day, side)
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
            if got["BID"] and got["ASK"]:
                data[day] = got
            else:
                empty += 1
        if (i + 1) % 100 == 0:
            logger.info("[NAKANE] 取得 %d/%d日", i + 1, len(targets))
    notes = [f"対象の営業日{len(targets)}日、取得{len(data)}日、データ無し{empty}日、失敗{len(failures)}件"] + failures[:5]
    if not data:
        raise RuntimeError("1分足が1日分も取得できませんでした: " + " / ".join(failures[:3]))
    return data, notes, start, end


# ---------------------------------------------------------------- 価格の取り出し

def _bar_at(bars: dict[int, tuple], minute: int):
    for m in range(minute, minute + ENTRY_TOLERANCE_MIN + 1):
        if m in bars:
            return m, bars[m]
    return None


def open_at(day_data, side: str, minute: int):
    found = _bar_at(day_data[side], minute)
    return None if found is None else found[1][0]


def mid_open(day_data, minute: int):
    bid, ask = open_at(day_data, "BID", minute), open_at(day_data, "ASK", minute)
    return None if bid is None or ask is None else (bid + ask) / 2


def classify(day: date, gotobi: set[date]) -> str:
    if day in gotobi:
        return "G"
    return "F" if day.weekday() == 4 else "N"


def in_group(group: str, kind: str) -> bool:
    return group == "ALL" or kind == group or (group == "GF" and kind in ("G", "F"))


# ---------------------------------------------------------------- 集計

def stats(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": 0.0, "median": 0.0, "win": 0.0, "t": 0.0, "sum": 0.0, "worst": 0.0}
    mean = sum(values) / n
    sd = statistics.stdev(values) if n > 1 else 0.0
    return {"n": n, "mean": mean, "median": statistics.median(values), "win": sum(1 for v in values if v > 0) / n * 100,
            "t": mean / (sd / n ** 0.5) if sd > 0 else 0.0, "sum": sum(values), "worst": min(values)}


def window_moves(data, kinds, start_min: int, end_min: int) -> dict[str, list[float]]:
    """グループ(G/F/N)ごとの、中値の始値→始値の変化(pips)。"""
    out: dict[str, list[float]] = {"G": [], "F": [], "N": []}
    for day, d in data.items():
        a, b = mid_open(d, start_min), mid_open(d, end_min)
        if a is not None and b is not None:
            out[kinds[day]].append((b - a) / PIP)
    return out


def simulate_long(day_data, entry_min: int, exit_min: int, stop_pips, dip_filter: bool):
    """(手数料前の損益pips, 手数料後の損益pips, 損切りか) または None(約定できない日)。"""
    entry = open_at(day_data, "ASK", entry_min)
    exit_price = open_at(day_data, "BID", exit_min)
    if entry is None or exit_price is None:
        return None
    if dip_filter:
        base, now = mid_open(day_data, M(6, 0)), mid_open(day_data, entry_min)
        if base is None or now is None or not now < base:
            return None
    stopped = False
    if stop_pips is not None:
        level = entry - stop_pips * PIP
        for minute in range(entry_min, exit_min):
            bar = day_data["BID"].get(minute)
            if bar is not None and bar[2] <= level:
                exit_price, stopped = min(level, bar[0]) if bar[0] <= level else level, True
                break
    gross = (exit_price - entry) / PIP
    return gross, gross - 2 * API_FEE_PIPS_PER_FILL, stopped


def simulate_short(day_data, entry_min: int, exit_min: int, stop_pips):
    entry = open_at(day_data, "BID", entry_min)
    exit_price = open_at(day_data, "ASK", exit_min)
    if entry is None or exit_price is None:
        return None
    stopped = False
    if stop_pips is not None:
        level = entry + stop_pips * PIP
        for minute in range(entry_min, exit_min):
            bar = day_data["ASK"].get(minute)
            if bar is not None and bar[1] >= level:
                exit_price, stopped = max(level, bar[0]) if bar[0] >= level else level, True
                break
    gross = (entry - exit_price) / PIP
    return gross, gross - 2 * API_FEE_PIPS_PER_FILL, stopped


def run_variant(data, kinds, group: str, simulate):
    gross, net, stops, by_year, days_used = [], [], 0, {}, 0
    for day in sorted(data):
        if not in_group(group, kinds[day]):
            continue
        res = simulate(data[day])
        if res is None:
            continue
        g, n_, stopped = res
        gross.append(g)
        net.append(n_)
        stops += stopped
        by_year.setdefault(day.year, []).append(n_)
        days_used += 1
    return {"gross": stats(gross), "net": stats(net), "stop_rate": stops / days_used * 100 if days_used else 0.0,
            "by_year": {y: stats(v) for y, v in sorted(by_year.items())}}


def spread_summary(data) -> list[str]:
    lines = []
    for minute in (M(8, 0), M(9, 0), M(9, 54), M(9, 55), M(10, 30)):
        spreads = []
        for d in data.values():
            bid, ask = open_at(d, "BID", minute), open_at(d, "ASK", minute)
            if bid is not None and ask is not None:
                spreads.append((ask - bid) / PIP)
        if spreads:
            s = sorted(spreads)
            lines.append(f"{hm(minute)} 中央値{statistics.median(s):.1f} 90%点{s[int(len(s) * 0.9)]:.1f} 最大{s[-1]:.1f}")
    return lines


def _fmt(st: dict) -> str:
    return f"{st['mean']:+.1f}({st['win']:.0f}%,t{st['t']:+.1f},n{st['n']})"


def build_reports(data, header: str):
    """(ログ用の詳細, Telegram用の短い文) を返す。"""
    days = sorted(data)
    gotobi = gotobi_days(days[0], days[-1])
    kinds = {d: classify(d, gotobi) for d in days}
    counts = {k: sum(1 for v in kinds.values() if v == k) for k in ("G", "F", "N")}
    detail = [header, f"営業日の内訳: ゴトー日{counts['G']}・金曜{counts['F']}・その他{counts['N']}(計{len(days)}日)",
              "売買差(pips、ASK-BID): " + " / ".join(spread_summary(data))]
    short = [header, f"営業日: ゴトー日{counts['G']}・金曜{counts['F']}・その他{counts['N']}",
             "スプレッド(pips)中央値: " + " / ".join(l.split(" 中央値")[0] + ":" + l.split("中央値")[1].split(" ")[0]
                                                  for l in spread_summary(data))]

    detail.append("【A】時間帯の値動き(中値、手数料前) 平均pips(上昇した日の割合,t値,n)")
    short += ["", "【A】時間帯の値動き(中値・手数料前) 平均pips(勝率,t値,日数)"]
    for start_min, end_min in WINDOWS:
        moves = window_moves(data, kinds, start_min, end_min)
        line = f"{hm(start_min)}→{hm(end_min)}: G {_fmt(stats(moves['G']))} / F {_fmt(stats(moves['F']))} / N {_fmt(stats(moves['N']))}"
        detail.append(line)
        short.append(line)

    detail.append("【B】ロング(ASKで買い・BIDで決済) 手数料後pips[手数料前]")
    short += ["", "【B】ロング 1回あたり手数料後pips(勝率,t値,日数) 往復API手数料0.4pips込み"]
    for entry_min in (M(8, 0), M(9, 0)):
        for group, gname in GROUPS:
            for stop in STOPS_LONG:
                res = run_variant(data, kinds, group, lambda d, e=entry_min, s=stop: simulate_long(d, e, M(9, 54), s, False))
                line = (f"買{hm(entry_min)}→9:54 {group} 損切り{'なし' if stop is None else f'-{stop}'}: {_fmt(res['net'])} "
                        f"[手数料前{res['gross']['mean']:+.1f}] 中央値{res['net']['median']:+.1f} 最悪{res['net']['worst']:+.0f} "
                        f"損切り{res['stop_rate']:.0f}% 年別" + " ".join(f"{y}:{v['mean']:+.1f}" for y, v in res['by_year'].items()))
                detail.append(line)
                if stop in (None, 30):
                    short.append(f"買{hm(entry_min)} {group} SL{'なし' if stop is None else f'-{stop}'}: {_fmt(res['net'])}")
        for stop in STOPS_LONG:
            res = run_variant(data, kinds, "G", lambda d, e=entry_min, s=stop: simulate_long(d, e, M(9, 54), s, True))
            line = f"買{hm(entry_min)}→9:54 G+押し目 損切り{'なし' if stop is None else f'-{stop}'}: {_fmt(res['net'])} [手数料前{res['gross']['mean']:+.1f}]"
            detail.append(line)
            if stop is None:
                short.append(f"買{hm(entry_min)} G+押し目 SLなし: {_fmt(res['net'])}")

    detail.append("【C】ショート(BIDで売り・ASKで決済) 手数料後pips")
    short += ["", "【C】ドテン売り(9:55に売り) 手数料後pips"]
    for exit_min in (M(10, 30), M(11, 0)):
        for group, gname in GROUPS:
            for stop in STOPS_SHORT:
                res = run_variant(data, kinds, group, lambda d, x=exit_min, s=stop: simulate_short(d, M(9, 55), x, s))
                line = (f"売9:55→{hm(exit_min)} {group} 損切り{'なし' if stop is None else f'-{stop}'}: {_fmt(res['net'])} "
                        f"[手数料前{res['gross']['mean']:+.1f}] 最悪{res['net']['worst']:+.0f} 損切り{res['stop_rate']:.0f}% "
                        f"年別" + " ".join(f"{y}:{v['mean']:+.1f}" for y, v in res['by_year'].items()))
                detail.append(line)
                if stop is None:
                    short.append(f"売→{hm(exit_min)} {group}: {_fmt(res['net'])}")
    short += ["", "※t値は多重比較で偶然に大きくなりやすい。2を超えても、そのまま信じない。約定は1分足の始値の近似。"]
    return "\n".join(detail), "\n".join(short)


def run_nakane(days: int):
    started = time.monotonic()
    notify(f"🧪 仲値トレードの検証を開始します(直近{days}日・USD/JPY 1分足)。データ取得に約25〜40分かかります。")
    data, notes, start, end = fetch_all(days)
    header = f"USD/JPY 1分足(GMOコインFX) {start}〜{end} 取得{len(data)}日"
    for note in notes:
        logger.info("[NAKANE] %s", note)
    detail, short = build_reports(data, header)
    for line in detail.splitlines():
        logger.info("[NAKANE] %s", line)
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
        run_nakane(days)
    except Exception as exc:
        logger.exception("[NAKANE] 検証に失敗しました")
        notify(f"仲値トレードの検証に失敗しました: {_describe_error(exc)}", level="error")


def run_nakane_in_background(days: int):
    threading.Thread(target=_safe_run, args=(days,), daemon=True, name="nakane-compare").start()
