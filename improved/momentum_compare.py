"""
「直近で上がった銘柄を買う」(モメンタム)が、国内取引所で取引できる銘柄で成り立つかを、日足で確かめる検証。

使い方: MOMENTUM_BACKTEST_YEARS に1以上(最大8)を設定して起動すると、起動後に別スレッドで1回だけ
 1. bitbankの公開APIから、JPY建ての全銘柄の一覧と、各銘柄の日足(年単位)を取得
 2. 毎週(または2週間ごと)に、直近L日の上昇率が上位K銘柄を等金額で買い、H日保有する戦略をシミュレーション
    (比較用に、下位K銘柄を買う「反転」、全銘柄の等金額、BTC買い持ちも計算)
 3. 結果をログ([MOMENTUM])とTelegramに出す
を行う(0なら何もしない。終わったら0に戻すこと)。ライブの取引ロジックには一切手を入れない。

試すパターンは事前に決めた固定の組み合わせ(L=7/14日、H=7/14日、K=1/3、上位/下位)だけで、
結果を見てから足さない(過剰最適化を避けるため)。

近似・限界(結果を読むときの注意):
- 「いまbitbankに上場している銘柄」だけが対象。上場廃止になった銘柄は含まれない(生存者バイアス。
  成績が実際より良く出る方向に偏る)。
- 上場直後の値動き(上場ポンプ)を避けるため、上場から60日たち、直近30日の売買代金(中央値)が
  2,000万円以上の銘柄だけを対象にする。
- 約定は日足の終値、手数料+売買差は片道の固定率(0.15%と0.30%の2通り。アルトコインの実際の
  コストは未確認)。大きな注文によるスリッページは考えない。
- 売りから入る戦略は、国内の現物ではできないので、買いだけ。
"""

import logging
import statistics
import threading
import time
from datetime import datetime, timezone

import requests

import config
from improved.notifier import notify
from improved.spread_probe import _describe_error

logger = logging.getLogger(__name__)

DAY_MS = 86_400_000
FORWARD_FILL_DAYS = 3
MIN_HISTORY_DAYS = 60
LIQUIDITY_WINDOW_DAYS = 30
MIN_MEDIAN_JPY_VOLUME = 20_000_000
COST_PER_SIDE_PCT = (0.15, 0.30)
LOOKBACKS = (7, 14)
HOLDS = (7, 14)
TOP_COUNTS = (1, 3)
MAX_FAILURE_RATE = 0.10


# ---------------------------------------------------------------- データ取得

def _get_json(url):
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_jpy_pairs() -> list[str]:
    payload = _get_json("https://public.bitbank.cc/tickers_jpy")
    if payload.get("success") != 1:
        raise ValueError(f"bitbankの銘柄一覧がエラーを返しました: {payload.get('data')}")
    return sorted(item["pair"] for item in payload["data"])


class NoData(Exception):
    """その年のデータが無い(上場前など)。"""


def fetch_daily(pair: str, year: int) -> list[tuple[int, float, float]]:
    """(日番号, 終値, 売買代金[円])のリスト。日番号=UTC換算のエポック日。"""
    resp = requests.get(f"https://public.bitbank.cc/{pair}/candlestick/1day/{year}", timeout=15)
    if resp.status_code == 404:
        # 上場前の年などは、bitbankが404(success=0, code=10000)を返す。データ無しとして扱う。
        raise NoData(resp.text[:80])
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("success") != 1:
        raise NoData(str(payload.get("data")))
    candles = payload["data"]["candlestick"]
    if not candles:
        return []
    rows = []
    for row in candles[0]["ohlcv"]:
        close, volume = float(row[3]), float(row[4])
        if close > 0:
            rows.append((int(row[5]) // DAY_MS, close, close * volume))
    return rows


def fetch_all(years: int, sleep_sec: float = 0.3):
    """{pair: {day: (close, jpy_volume)}} と、取得失敗などのメモを返す。"""
    pairs = fetch_jpy_pairs()
    this_year = datetime.now(timezone.utc).year
    year_list = list(range(this_year - years + 1, this_year + 1))
    data: dict[str, dict[int, tuple[float, float]]] = {}
    failures, empty, total = [], 0, 0
    expected_total = len(pairs) * len(year_list)
    for pair in pairs:
        if len(failures) > expected_total * MAX_FAILURE_RATE:
            break   # 失敗が多すぎる: 以降の取得を打ち切る(下で例外にする)
        for year in year_list:
            total += 1
            last_exc = None
            rows = []
            for attempt in range(3):
                try:
                    rows = fetch_daily(pair, year)
                    last_exc = None
                    break
                except NoData:
                    empty += 1
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    time.sleep(1.0 * (attempt + 1))
            if last_exc is not None:
                failures.append(f"{pair}/{year}: {_describe_error(last_exc)}")
            for day, close, volume in rows:
                data.setdefault(pair, {})[day] = (close, volume)
            time.sleep(sleep_sec)
    notes = [f"{len(pairs)}銘柄×{len(year_list)}年={total}回取得、データ無し{empty}回、失敗{len(failures)}回"]
    if len(failures) > expected_total * MAX_FAILURE_RATE:
        raise RuntimeError(f"取得失敗が多すぎます({len(failures)}/{expected_total}回): " + " / ".join(failures[:3]))
    notes.extend(failures[:5])
    if not data:
        raise RuntimeError("日足が1本も取得できませんでした")
    return data, notes


# ---------------------------------------------------------------- 整形

class Market:
    """日付をそろえた価格表。closes[pair][i]=その日の終値(欠測は3日まで前日の値で埋め、それ以上はNone)。"""

    def __init__(self, data: dict[str, dict[int, tuple[float, float]]]):
        self.start_day = min(min(d) for d in data.values())
        self.end_day = max(max(d) for d in data.values())
        n = self.end_day - self.start_day + 1
        self.n_days = n
        self.pairs = sorted(data)
        self.closes: dict[str, list] = {}
        self.volumes: dict[str, list[float]] = {}
        self.first_idx: dict[str, int] = {}
        for pair, rows in data.items():
            closes: list = [None] * n
            volumes = [0.0] * n
            last_close, last_idx = None, -10**9
            for i in range(n):
                row = rows.get(self.start_day + i)
                if row is not None:
                    closes[i], volumes[i] = row
                    last_close, last_idx = row[0], i
                elif last_close is not None and i - last_idx <= FORWARD_FILL_DAYS:
                    closes[i] = last_close
            self.closes[pair], self.volumes[pair] = closes, volumes
            self.first_idx[pair] = min(self.start_day_index(d) for d in rows)

    def start_day_index(self, day: int) -> int:
        return day - self.start_day

    def price_at_or_before(self, pair: str, idx: int):
        arr = self.closes[pair]
        for j in range(min(idx, self.n_days - 1), -1, -1):
            if arr[j] is not None:
                return arr[j]
        return None

    def eligible(self, idx: int, lookback: int) -> list[str]:
        """idx日に買える銘柄: 直近lookback日前の価格もあり、上場から60日以上、売買代金が十分。"""
        out = []
        for pair in self.pairs:
            closes = self.closes[pair]
            if idx - lookback < 0 or closes[idx] is None or closes[idx - lookback] is None:
                continue
            if idx - self.first_idx[pair] < MIN_HISTORY_DAYS:
                continue
            window = self.volumes[pair][max(0, idx - LIQUIDITY_WINDOW_DAYS + 1) : idx + 1]
            if statistics.median(window) < MIN_MEDIAN_JPY_VOLUME:
                continue
            out.append(pair)
        return out


# ---------------------------------------------------------------- シミュレーション

def simulate(market: Market, lookback: int, hold: int, top_k, reverse: bool, cost_side_pct: float,
             start_idx: int, end_idx: int) -> dict:
    """
    start_idx〜end_idx(日の添字)のあいだ、hold日ごとに、直近lookback日の上昇率で並べた上位(reverse=Trueなら下位)
    top_k銘柄を等金額で買い、hold日保有する。top_k=Noneなら、買える全銘柄の等金額。
    """
    cost = cost_side_pct / 100
    equity, peak, max_dd = 1.0, 1.0, 0.0
    prev_weights: dict[str, float] = {}
    period_returns, picks = [], []
    i = start_idx
    while i + hold <= end_idx:
        candidates = market.eligible(i, lookback)
        if candidates:
            ranked = sorted(candidates, key=lambda p: market.closes[p][i] / market.closes[p][i - lookback])
            if top_k is None:
                chosen = ranked
            else:
                chosen = ranked[:top_k] if reverse else ranked[-top_k:]
        else:
            chosen = []
        weights = {p: 1.0 / len(chosen) for p in chosen}
        turnover = sum(abs(weights.get(p, 0.0) - prev_weights.get(p, 0.0)) for p in set(weights) | set(prev_weights))
        equity *= 1 - cost * turnover
        gross = 0.0
        for p, w in weights.items():
            exit_price = market.price_at_or_before(p, i + hold)
            gross += w * (exit_price / market.closes[p][i] - 1)
        equity *= 1 + gross
        period_returns.append(gross - cost * turnover)
        picks.append(chosen)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
        prev_weights = weights
        i += hold
    equity *= 1 - cost * sum(prev_weights.values())   # 最後に全部売るコスト
    wins = sum(1 for r in period_returns if r > 0)
    return {
        "total_pct": (equity - 1) * 100,
        "periods": len(period_returns),
        "win_pct": wins / len(period_returns) * 100 if period_returns else 0.0,
        "avg_pct": sum(period_returns) / len(period_returns) * 100 if period_returns else 0.0,
        "max_dd_pct": max_dd * 100,
        "picks": picks,
    }


def btc_hold(market: Market, start_idx: int, end_idx: int, cost_side_pct: float = 0.0, pair: str = "btc_jpy") -> float:
    p0, p1 = market.closes[pair][start_idx], market.price_at_or_before(pair, end_idx)
    cost = cost_side_pct / 100
    return (p1 / p0 * (1 - cost) ** 2 - 1) * 100


# ---------------------------------------------------------------- レポート

def build_reports(market: Market, header: str):
    benchmark = "btc_jpy" if "btc_jpy" in market.closes else market.pairs[0]
    max_lb = max(LOOKBACKS)
    start_idx = max(market.first_idx[benchmark] + MIN_HISTORY_DAYS, max_lb)
    end_idx = market.n_days - 1
    mid = (start_idx + end_idx) // 2
    windows = {"全体": (start_idx, end_idx), "前半": (start_idx, mid), "後半": (mid, end_idx)}
    span = f"{datetime.fromtimestamp((market.start_day + start_idx) * DAY_MS / 1000, timezone.utc):%Y-%m-%d}〜" \
           f"{datetime.fromtimestamp((market.start_day + end_idx) * DAY_MS / 1000, timezone.utc):%Y-%m-%d}"
    bh = {n: btc_hold(market, a, b) for n, (a, b) in windows.items()}
    detail = [header, f"検証期間 {span} / BTC買い持ち 全体{bh['全体']:+.1f}% 前半{bh['前半']:+.1f}% 後半{bh['後半']:+.1f}%"]
    short = [header, f"期間 {span}", f"BTC買い持ち 全{bh['全体']:+.1f}% 前{bh['前半']:+.1f}% 後{bh['後半']:+.1f}%",
             f"表記: 全体(期間数・勝率) 前半/後半 | 片道0.30%の全体。コスト片道0.15%",
             "上=上昇率上位を買う、下=下落率上位を買う(反転)、全=全銘柄等金額"]

    def row(label, lookback, hold, top_k, reverse):
        res = {}
        for cost in COST_PER_SIDE_PCT:
            for name, (a, b) in windows.items():
                res[(cost, name)] = simulate(market, lookback, hold, top_k, reverse, cost, a, b)
        low, high = COST_PER_SIDE_PCT
        whole = res[(low, "全体")]
        line = (f"{label}: {whole['total_pct']:+.1f}%({whole['periods']}期間・{whole['win_pct']:.0f}%) "
                f"前{res[(low, '前半')]['total_pct']:+.1f}/後{res[(low, '後半')]['total_pct']:+.1f}% | "
                f"{res[(high, '全体')]['total_pct']:+.1f}%")
        extra = (f" 平均{whole['avg_pct']:+.2f}%/期間 DD{whole['max_dd_pct']:.0f}% "
                 f"買った銘柄数{len({p for ps in whole['picks'] for p in ps})}")
        return line, extra

    for hold in HOLDS:
        short.append(f"■保有{hold}日")
        detail.append(f"■保有{hold}日")
        line, extra = row(f"全銘柄 直近7日", 7, hold, None, False)
        short.append(line); detail.append(line + extra)
        for lookback in LOOKBACKS:
            for top_k in TOP_COUNTS:
                for reverse in (False, True):
                    label = f"{lookback}日{'下' if reverse else '上'}位{top_k}"
                    line, extra = row(label, lookback, hold, top_k, reverse)
                    short.append(line); detail.append(line + extra)
    return "\n".join(detail), "\n".join(short)


def run_momentum(years: int):
    started = time.monotonic()
    notify(f"🧪 日足モメンタム検証を開始します(直近{years}年・bitbankのJPY銘柄)。結果は終了後に通知します。")
    data, notes = fetch_all(years)
    market = Market(data)
    eligible_now = len(market.eligible(market.n_days - 1, max(LOOKBACKS)))
    header = (f"日足モメンタム検証 bitbank JPY {len(market.pairs)}銘柄(うち直近の対象{eligible_now}銘柄)")
    logger.info("[MOMENTUM] %s", header)
    for note in notes:
        logger.info("[MOMENTUM] %s", note)
    detail, short = build_reports(market, header)
    for line in detail.splitlines():
        logger.info("[MOMENTUM] %s", line)
    short += f"\n(所要{time.monotonic() - started:.0f}秒。上場廃止銘柄を含まないため、成績は実際より良く出る方向に偏ります)"
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


def _safe_run(years: int):
    try:
        run_momentum(years)
    except Exception as exc:
        logger.exception("[MOMENTUM] 検証に失敗しました")
        notify(f"日足モメンタム検証に失敗しました: {_describe_error(exc)}", level="error")


def run_momentum_in_background(years: int):
    threading.Thread(target=_safe_run, args=(years,), daemon=True, name="momentum-compare").start()
