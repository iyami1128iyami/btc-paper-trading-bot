"""
移動平均線クロス戦略

短期移動平均線が長期移動平均線を
- 下から上に抜けたら → ゴールデンクロス → 買いシグナル
- 上から下に抜けたら → デッドクロス → 売りシグナル
"""

from config import SHORT_WINDOW, LONG_WINDOW


def moving_average(prices: list[float], window: int) -> float:
    """直近window本分の単純移動平均を計算する"""
    if len(prices) < window:
        return None
    return sum(prices[-window:]) / window


def generate_signal(prices: list[float]) -> str:
    """
    価格リストから売買シグナルを判定する。

    Returns:
        "BUY", "SELL", "HOLD" のいずれか
    """
    if len(prices) < LONG_WINDOW + 1:
        # クロス判定には「直前」と「現在」の2時点分の移動平均が必要
        return "HOLD"

    # 現在時点の移動平均
    short_now = moving_average(prices, SHORT_WINDOW)
    long_now = moving_average(prices, LONG_WINDOW)

    # 1本前の時点の移動平均(クロスの瞬間を検出するため)
    prices_prev = prices[:-1]
    short_prev = moving_average(prices_prev, SHORT_WINDOW)
    long_prev = moving_average(prices_prev, LONG_WINDOW)

    if short_prev is None or long_prev is None:
        return "HOLD"

    golden_cross = short_prev <= long_prev and short_now > long_now
    dead_cross = short_prev >= long_prev and short_now < long_now

    if golden_cross:
        return "BUY"
    elif dead_cross:
        return "SELL"
    else:
        return "HOLD"
