"""日本の銀行営業日と、ゴトー日(5・10・15・20・25・30日。休業日なら前営業日)の判定。

祝日は、法律の規則(固定日・ハッピーマンデー・春分/秋分・振替休日・国民の休日)から計算する。
春分・秋分の近似式は1980〜2099年で有効。銀行の休業日は、日曜・祝日・12/31〜1/3とする。
"""

from datetime import date, timedelta

GOTOBI_NOMINAL_DAYS = (5, 10, 15, 20, 25, 30)


def _nth_monday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(0 - first.weekday()) % 7 + 7 * (n - 1))


def _spring_equinox_day(year: int) -> int:
    return int(20.8431 + 0.242194 * (year - 1980)) - int((year - 1980) / 4)


def _autumn_equinox_day(year: int) -> int:
    return int(23.2488 + 0.242194 * (year - 1980)) - int((year - 1980) / 4)


_cache: dict[int, frozenset] = {}


def japanese_holidays(year: int) -> frozenset:
    if year in _cache:
        return _cache[year]
    base = {
        date(year, 1, 1),
        _nth_monday(year, 1, 2),                                  # 成人の日
        date(year, 2, 11),                                        # 建国記念の日
        date(year, 2, 23),                                        # 天皇誕生日(2020年〜)
        date(year, 3, _spring_equinox_day(year)),                 # 春分の日
        date(year, 4, 29),                                        # 昭和の日
        date(year, 5, 3), date(year, 5, 4), date(year, 5, 5),     # 憲法記念日・みどりの日・こどもの日
        _nth_monday(year, 7, 3),                                  # 海の日
        date(year, 8, 11),                                        # 山の日
        _nth_monday(year, 9, 3),                                  # 敬老の日
        date(year, 9, _autumn_equinox_day(year)),                 # 秋分の日
        _nth_monday(year, 10, 2),                                 # スポーツの日
        date(year, 11, 3),                                        # 文化の日
        date(year, 11, 23),                                       # 勤労感謝の日
    }
    holidays = set(base)
    for h in sorted(base):                                        # 振替休日: 日曜の祝日は、次の祝日でない日
        if h.weekday() == 6:
            d = h + timedelta(days=1)
            while d in holidays:
                d += timedelta(days=1)
            holidays.add(d)
    d = date(year, 1, 2)
    while d < date(year, 12, 31):                                 # 国民の休日: 祝日にはさまれた平日
        if d not in holidays and d.weekday() != 6 and (d - timedelta(days=1)) in holidays \
                and (d + timedelta(days=1)) in holidays:
            holidays.add(d)
        d += timedelta(days=1)
    _cache[year] = frozenset(holidays)
    return _cache[year]


def is_bank_holiday(d: date) -> bool:
    if d.weekday() >= 5 or d in japanese_holidays(d.year):
        return True
    return (d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3)


def is_business_day(d: date) -> bool:
    return not is_bank_holiday(d)


def gotobi_days(start: date, end: date) -> set[date]:
    """start〜endに含まれるゴトー日(休業日なら前営業日に前倒し)の集合。"""
    out: set[date] = set()
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        for nominal in GOTOBI_NOMINAL_DAYS:
            try:
                d = date(year, month, nominal)
            except ValueError:                                    # 2月30日など
                continue
            while is_bank_holiday(d):
                d -= timedelta(days=1)
            out.add(d)
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return {d for d in out if start <= d <= end}
