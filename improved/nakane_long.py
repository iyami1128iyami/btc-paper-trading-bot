"""
仲値後のドテン売り(ゴトー日の9:55に売り、10:30に買い戻し)が、GMOコインのデータ期間(2023年10月28日〜)より
前にも成り立っていたかを、Dukascopyの公開データ(ティック、BID/ASK)で確かめる検証。

使い方: NAKANE_LONG_YEARS に1以上(最大12)を設定して起動すると、起動後に別スレッドで1回だけ
 1. 2023年10月27日から遡るN年ぶんの銀行営業日について、USD/JPYのティックデータ(UTCの0時台・1時台=日本時間9〜11時)を取得
 2. 9:50〜10:40(日本時間)の1分ごとの始値(BID/ASK)を作り、ゴトー日(G)とそれ以外(N)で比較
 3. 結果をログ([NAKANE-LONG])とTelegramに出す
を行う(0なら何もしない。終わったら0に戻すこと)。ライブの取引ロジックには一切手を入れない。

データ: https://datafeed.dukascopy.com/datafeed/USDJPY/{年}/{月-1:02d}/{日:02d}/{時:02d}h_ticks.bi5
 (月は0始まり、時刻はUTC、LZMA圧縮、1ティック20バイト=時刻ms・ASK・BID・ASK出来高・BID出来高)。
 価格の倍率(1000か100000)は、デコード結果が50〜250の範囲に収まるものを自動で選ぶ。404は「データなし」。
 公開データの形式は、公開されているライブラリの説明に基づく(実際の取得は未確認)。利用条件は未確認(個人の検証用途に限る)。

事前に決めた評価(新しい仮説を足さない。1つのルールの再現を見る):
 基準: ゴトー日の9:55に売り、10:30に買い戻す。損益は、中値の動きから、固定コスト0.8pips
       (GMOコインの実測スプレッドの平均0.4pips+API手数料0.4pips)を引いて計算する。
 「再現した」とみなす条件(5項目):
   1. 基準の平均が0超で、95%信頼区間(ブートストラップ)の下限も0超
   2. ゴトー日が「ランダムな日」より特別かの並べ替え検定で、p<0.05
   3. 年ごとの平均がプラスの年が、6割以上
   4. 9:55→10:30の中値の動きが、ゴトー日のほうがそれ以外の日より下がっている(Welchのt≦-2)
   5. 9:54→9:57の中値の動きが、ゴトー日で下落(平均<0、t≦-2)
 (GMOコイン期間の結果: 基準+2.5pips、9:54→9:57は-2.1pips)

取得の方針: 1本ずつ(待ち0.6秒)取得し、接続エラーは待ってから再試行する。20日連続で失敗したら、そこで止めて、
 取れた分(120日以上)で分析する。取得できた日は、Redisに保存し、次回の実行で再開できる。

近似・限界: DukascopyのレートはGMOコインとは別の会社(ECN)のレート。固定コストは、GMOの実測値の仮定。
 ティックから1分の「始値」(その分の最初のティック。なければ直前の値)を作る近似。日本の祝日はコードで計算(2007〜2026年に対応)。
"""

import json
import logging
import lzma
import statistics
import struct
import threading
import time
import zlib
from datetime import date, timedelta

import requests

import config
from improved.jp_calendar import gotobi_days, is_business_day
from improved.nakane_compare import PIP, bootstrap_ci, classify, permutation_p, stats
from improved.notifier import notify
from improved.spread_probe import _describe_error

logger = logging.getLogger(__name__)

URL = "https://datafeed.dukascopy.com/datafeed/USDJPY/{y:04d}/{m0:02d}/{d:02d}/{h:02d}h_ticks.bi5"
END_DATE = date(2023, 10, 27)          # GMOコインのデータの直前
JST_OFFSET_MIN = 9 * 60
HOURS_UTC = (0, 1)                     # 日本時間 9:00〜11:00
M = lambda h, m: h * 60 + m
MINUTES = range(M(9, 50), M(10, 40) + 1)
FIXED_COST_PIPS = 0.8
BASE_ENTRY, BASE_EXIT = M(9, 55), M(10, 30)
REQUEST_SLEEP_SEC = 0.6                 # 1ファイルごとの待ち。Dukascopyは、速すぎると接続を切る(2026-11の実測)
RETRY_BACKOFF_SEC = (2, 5, 10, 20)      # 接続エラー・5xxの再試行までの待ち(最大4回)
MAX_CONSECUTIVE_FAILURES = 20           # これだけ連続で失敗したら、ブロックされたとみなして取得を止める
MAX_FAILURE_RATE = 0.25
MIN_DAYS_FOR_PARTIAL = 120              # 途中で止まっても、これだけ取れていれば、そこまでで分析する
REQUIRED_MINUTES = (M(9, 54), M(9, 55), M(9, 57), M(10, 30))
CACHE_KEY = "nakane_long:cache:v1"
CACHE_MAX_BYTES = 3_000_000
CACHE_TTL_SEC = 90 * 86400
DIVISORS = (1000, 100000)               # 円ペアは小数3桁(1000)か5桁(100000)
PRICE_RANGE = (50.0, 250.0)


# ---------------------------------------------------------------- デコード

def decode_bi5(blob: bytes):
    """(ms, bid, ask)のリストを返す。圧縮が壊れている・価格の倍率が判定できない場合は例外。"""
    raw = lzma.decompress(blob)
    n = len(raw) // 20
    if n == 0:
        return []
    rows = [struct.unpack_from(">Iiiff", raw, i * 20) for i in range(n)]
    sample = rows[:20]
    for divisor in DIVISORS:
        if all(PRICE_RANGE[0] < r[1] / divisor < PRICE_RANGE[1] and PRICE_RANGE[0] < r[2] / divisor < PRICE_RANGE[1]
               for r in sample):
            break
    else:
        raise ValueError(f"価格の倍率を判定できません(先頭のASK/BID={sample[0][1]}/{sample[0][2]})")
    ticks = [(r[0], r[2] / divisor, r[1] / divisor) for r in rows]     # (ms, bid, ask) ※ファイルの順はASK→BID
    if sum(1 for _, bid, ask in ticks if bid > ask) > len(ticks) / 2:   # 順序が逆なら入れ替える
        ticks = [(ms, ask, bid) for ms, bid, ask in ticks]
    return ticks


def fetch_hour(day: date, hour: int):
    """その日のUTC hour時台のティックを返す。404は「データなし」(空リスト)。"""
    url = URL.format(y=day.year, m0=day.month - 1, d=day.day, h=hour)
    resp = requests.get(url, timeout=30)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    if not resp.content:
        return []
    return decode_bi5(resp.content)


def minute_opens(hours_ticks: dict[int, list]):
    """{日本時間の分: (BIDの始値, ASKの始値)}。始値は、その分の最初のティック(なければ直前の値)。"""
    all_ticks = []
    for hour, ticks in hours_ticks.items():
        base = hour * 3_600_000
        all_ticks.extend((base + ms, bid, ask) for ms, bid, ask in ticks)
    all_ticks.sort(key=lambda t: t[0])
    if not all_ticks:
        return {}
    out = {}
    idx, last = 0, None
    for minute in range(M(9, 0), M(11, 0) + 1):
        start_ms = (minute - JST_OFFSET_MIN) * 60_000      # UTCの0時からの経過ミリ秒
        first_in_minute = None
        while idx < len(all_ticks) and all_ticks[idx][0] < start_ms + 60_000:
            if all_ticks[idx][0] >= start_ms and first_in_minute is None:
                first_in_minute = all_ticks[idx]
            last = all_ticks[idx]
            idx += 1
        chosen = first_in_minute or last
        if chosen is not None and minute in MINUTES:
            out[minute] = (chosen[1], chosen[2])
    return out


def fetch_day(day: date):
    hours = {}
    for hour in HOURS_UTC:
        last_exc = None
        for attempt in range(len(RETRY_BACKOFF_SEC) + 1):
            try:
                hours[hour] = fetch_hour(day, hour)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if attempt < len(RETRY_BACKOFF_SEC):
                    time.sleep(RETRY_BACKOFF_SEC[attempt])
        if last_exc is not None:
            raise last_exc
        time.sleep(REQUEST_SLEEP_SEC)
    opens = minute_opens(hours)
    return opens if len(opens) >= len(MINUTES) * 0.8 else None


# ---------------------------------------------------------------- 取得済みデータの保存(Redis。失敗しても無視)

def _redis_client():
    if not config.REDIS_URL:
        return None
    import redis

    return redis.from_url(config.REDIS_URL, decode_responses=False, socket_timeout=20, socket_connect_timeout=10)


def encode_cache(data: dict) -> bytes:
    payload = {d.isoformat(): {str(m): list(v) for m, v in opens.items()} for d, opens in data.items()}
    return zlib.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"), 6)


def decode_cache(blob: bytes) -> dict:
    raw = json.loads(zlib.decompress(blob).decode("utf-8"))
    return {date.fromisoformat(d): {int(m): tuple(v) for m, v in opens.items()} for d, opens in raw.items()}


def load_cache() -> dict:
    try:
        client = _redis_client()
        blob = client.get(CACHE_KEY) if client else None
        if not blob:
            return {}
        data = decode_cache(blob)
        logger.info("[NAKANE-LONG] 保存済みのデータを読み込みました(%d日)", len(data))
        return data
    except Exception as exc:
        logger.warning("[NAKANE-LONG] 保存済みデータを読めませんでした(無視): %s", _describe_error(exc))
        return {}


def save_cache(data: dict) -> None:
    try:
        client = _redis_client()
        if client is None or not data:
            return
        blob = encode_cache(data)
        if len(blob) > CACHE_MAX_BYTES:
            logger.warning("[NAKANE-LONG] 保存データが大きすぎるため保存しません(%d bytes)", len(blob))
            return
        client.set(CACHE_KEY, blob, ex=CACHE_TTL_SEC)
        logger.info("[NAKANE-LONG] データを保存しました(%d日、%d bytes)", len(data), len(blob))
    except Exception as exc:
        logger.warning("[NAKANE-LONG] データを保存できませんでした(無視): %s", _describe_error(exc))


def fetch_all(years: int, end: date = END_DATE, cache: dict | None = None, save=None):
    """順番に(1本ずつ)取得する。連続で失敗が続けば、そこで止めて、取れた分を返す。
    戻り値: (データ, メモ, 開始日, 終了日, 途中で止まったか)。"""
    start = end - timedelta(days=365 * years)
    targets = [d for d in (start + timedelta(days=i) for i in range((end - start).days + 1)) if is_business_day(d)]
    data, failures, empty, reused, consecutive, partial = {}, [], 0, 0, 0, False
    new_since_save, logged_sample = 0, False
    for i, day in enumerate(targets):
        if cache and day in cache:
            data[day] = cache[day]
            reused += 1
            continue
        try:
            opens = fetch_day(day)
            consecutive = 0
            if opens:
                data[day] = opens
                new_since_save += 1
                if not logged_sample and M(9, 55) in opens:
                    logged_sample = True
                    logger.info("[NAKANE-LONG] 取得の確認 %s 9:55 BID/ASK=%s 9:56=%s(分の数%d)", day, opens[M(9, 55)],
                                opens.get(M(9, 56)), len(opens))
            else:
                empty += 1
        except Exception as exc:
            failures.append(f"{day}: {_describe_error(exc)}")
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_FAILURES or len(failures) > max(10, len(targets) * MAX_FAILURE_RATE):
                partial = True
                break
        if (i + 1) % 100 == 0:
            logger.info("[NAKANE-LONG] 取得 %d/%d日(取得済み%d・失敗%d)", i + 1, len(targets), len(data), len(failures))
            if save and new_since_save:
                save(data)
                new_since_save = 0
    if save and new_since_save:
        save(data)
    if not data:
        raise RuntimeError("ティックデータが1日分も取得できませんでした: " + " / ".join(failures[:3]))
    notes = [f"対象の営業日{len(targets)}日、取得{len(data)}日(うち保存済み{reused}日)、データ無し{empty}日、失敗{len(failures)}件"
             + ("、連続失敗のため途中で停止" if partial else "")] + failures[:5]
    return data, notes, start, end, partial


# ---------------------------------------------------------------- 分析

def mid(opens, minute):
    bid, ask = opens[minute]
    return (bid + ask) / 2


def welch_t(a: list[float], b: list[float]) -> float:
    if len(a) < 2 or len(b) < 2:
        return 0.0
    var = statistics.variance(a) / len(a) + statistics.variance(b) / len(b)
    return (sum(a) / len(a) - sum(b) / len(b)) / var ** 0.5 if var > 0 else 0.0


def build_report(data, header: str):
    data = {d: o for d, o in data.items() if all(m in o for m in REQUIRED_MINUTES)}
    days = sorted(data)
    gotobi = gotobi_days(days[0], days[-1])
    kinds = {d: classify(d, gotobi) for d in days}
    g_days = [d for d in days if kinds[d] == "G"]
    n_days = [d for d in days if kinds[d] == "N"]
    all_nets = {d: (mid(data[d], BASE_ENTRY) - mid(data[d], BASE_EXIT)) / PIP - FIXED_COST_PIPS for d in days}
    g_nets = [all_nets[d] for d in g_days]
    st = stats(g_nets)
    lo, hi = bootstrap_ci(g_nets)
    p = permutation_p(g_nets, list(all_nets.values()))
    moves_g = [(mid(data[d], BASE_EXIT) - mid(data[d], BASE_ENTRY)) / PIP for d in g_days]
    moves_n = [(mid(data[d], BASE_EXIT) - mid(data[d], BASE_ENTRY)) / PIP for d in n_days]
    t_diff = welch_t(moves_g, moves_n)
    eff_g = stats([(mid(data[d], M(9, 57)) - mid(data[d], M(9, 54))) / PIP for d in g_days])
    eff_n = stats([(mid(data[d], M(9, 57)) - mid(data[d], M(9, 54))) / PIP for d in n_days])
    by_year: dict[int, list[float]] = {}
    for d in g_days:
        by_year.setdefault(d.year, []).append(all_nets[d])
    year_means = {y: stats(v) for y, v in sorted(by_year.items())}
    positive_years = sum(1 for v in year_means.values() if v["mean"] > 0)
    spreads = {}
    for minute in (BASE_ENTRY, BASE_EXIT):
        vals = sorted((data[d][minute][1] - data[d][minute][0]) / PIP for d in days)
        spreads[minute] = vals[len(vals) // 2]

    checks = [
        ("基準の平均>0で、信頼区間の下限も>0", st["mean"] > 0 and lo > 0),
        ("並べ替え検定p<0.05", p < 0.05),
        (f"年ごとの平均がプラスの年が6割以上({positive_years}/{len(year_means)})",
         len(year_means) > 0 and positive_years / len(year_means) >= 0.6),
        ("9:55→10:30の下落が、ゴトー日のほうが大きい(Welchのt≦-2)", t_diff <= -2),
        ("9:54→9:57の下落が、ゴトー日で有意(平均<0、t≦-2)", eff_g["mean"] < 0 and eff_g["t"] <= -2),
    ]
    passed = sum(1 for _, ok in checks if ok)
    lines = [
        header,
        f"営業日{len(days)}日(ゴトー日{len(g_days)}・その他{len(n_days)}) Dukascopyの中央値スプレッド 9:55={spreads[BASE_ENTRY]:.1f}・10:30={spreads[BASE_EXIT]:.1f}pips",
        f"■基準(ゴトー日 9:55売り→10:30、固定コスト{FIXED_COST_PIPS}pips込み): 平均{st['mean']:+.2f}pips 勝率{st['win']:.0f}% t{st['t']:+.1f} n{st['n']}",
        f"  95%信頼区間[{lo:+.2f},{hi:+.2f}] 並べ替え検定p={p:.3f}",
        f"■中値の動き(pips) 9:55→10:30 ゴトー日{stats(moves_g)['mean']:+.1f} / その他{stats(moves_n)['mean']:+.1f} (差のt{t_diff:+.1f})",
        f"  9:54→9:57 ゴトー日{eff_g['mean']:+.1f}(t{eff_g['t']:+.1f}) / その他{eff_n['mean']:+.1f}(t{eff_n['t']:+.1f})",
        "■年別(基準・ゴトー日の平均pips): " + " ".join(f"{y}:{v['mean']:+.1f}(n{v['n']})" for y, v in year_means.items()),
        f"■事前に決めた条件の達成: {passed}/5",
    ] + [f"  {'○' if ok else '×'} {name}" for name, ok in checks] + [
        "(GMOコイン期間 2023-11〜: 基準+2.5pips、9:54→9:57は-2.1pips)",
        "※別の会社(ECN)のレートで、固定コストは仮定。1つのルールの再現を見る検証。",
    ]
    return "\n".join(lines)


def run_nakane_long(years: int):
    started = time.monotonic()
    notify(f"🧪 仲値後のドテン売りの長期検証を開始します(2023年10月27日から遡る{years}年・Dukascopy)。"
           f"1本ずつ取得するため、数十分かかります(保存済みの分は省略)。")
    cache = load_cache()
    data, notes, start, end, partial = fetch_all(years, cache=cache, save=save_cache)
    for note in notes:
        logger.info("[NAKANE-LONG] %s", note)
    if partial and len(data) < MIN_DAYS_FOR_PARTIAL:
        raise RuntimeError(f"取得が途中で止まり、分析に必要な{MIN_DAYS_FOR_PARTIAL}日に届きませんでした(取得{len(data)}日): "
                           + " / ".join(notes[1:3]) + "。もう一度実行すると、保存済みの分から再開します")
    header = f"USD/JPY ティック(Dukascopy) {start}〜{end} 取得{len(data)}日" + ("【途中で停止。取れた分だけの分析】" if partial else "")
    report = build_report(data, header)
    if len(notes) > 1:
        report += "\n※取得の問題: " + " / ".join(notes[1:])[:300]
    report += f"\n(所要{time.monotonic() - started:.0f}秒)"
    for line in report.splitlines():
        logger.info("[NAKANE-LONG] %s", line)
    chunk, chunks = [], []
    for line in report.splitlines():
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
        run_nakane_long(years)
    except Exception as exc:
        logger.exception("[NAKANE-LONG] 検証に失敗しました")
        notify(f"仲値の長期検証に失敗しました: {_describe_error(exc)}", level="error")


def run_nakane_long_in_background(years: int):
    threading.Thread(target=_safe_run, args=(years,), daemon=True, name="nakane-long").start()
