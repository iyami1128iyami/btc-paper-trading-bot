"""日本の銀行営業日と、ゴトー日(5・10・15・20・25・30日。休業日なら前営業日)の判定。

祝日は、法律の規則(固定日・ハッピーマンデー・春分/秋分・振替休日・国民の休日)から計算する。
2007〜2026年に対応(2019年の特別の休日、2020・2021年のオリンピックに伴う移動を含む)。春分・秋分の近似式は1980〜2099年で有効。銀行の休業日は、日曜・祝日・12/31〜1/3とする。
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
        _nth_monday(year, 1, 2) if year >= 2000 else date(year, 1, 15),   # 成人の日
        date(year, 2, 11),                                        # 建国記念の日
        date(year, 3, _spring_equinox_day(year)),                 # 春分の日
        date(year, 4, 29),                                        # 昭和の日(2007年〜。それ以前はみどりの日)
        date(year, 5, 3), date(year, 5, 5),                       # 憲法記念日・こどもの日
        _nth_monday(year, 9, 3),                                  # 敬老の日(2003年〜)
        date(year, 9, _autumn_equinox_day(year)),                 # 秋分の日
        date(year, 11, 3),                                        # 文化の日
        date(year, 11, 23),                                       # 勤労感謝の日
    }
    if year >= 2007:
        base.add(date(year, 5, 4))                                # みどりの日(2007年〜。それ以前は国民の休日)
    if year >= 2020:
        base.add(date(year, 2, 23))                               # 天皇誕生日(2020年〜)
    elif 1989 <= year <= 2018:
        base.add(date(year, 12, 23))                              # 天皇誕生日(1989〜2018年)
    # 海の日・山の日・体育の日(スポーツの日): 2020・2021年は、オリンピックに合わせて移動した
    if year == 2020:
        base |= {date(2020, 7, 23), date(2020, 7, 24), date(2020, 8, 10)}
    elif year == 2021:
        base |= {date(2021, 7, 22), date(2021, 7, 23), date(2021, 8, 8)}
    else:
        base.add(_nth_monday(year, 7, 3))                         # 海の日
        if year >= 2016:
            base.add(date(year, 8, 11))                           # 山の日(2016年〜)
        if year <= 2019 or year >= 2022:
            base.add(_nth_monday(year, 10, 2))                    # 体育の日(〜2019年)・スポーツの日(2022年〜)
    if year == 2019:                                              # 改元に伴う特別の休日
        base |= {date(2019, 4, 30), date(2019, 5, 1), date(2019, 10, 22)}
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
