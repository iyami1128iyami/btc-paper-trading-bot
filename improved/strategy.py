"""
改善版：移動平均線クロス戦略

補助指標を追加：
- RSI（相対力指数）でオーバーバイ/オーバーソルド検出
- エントリー確度の向上
"""

import logging

from config import LONG_WINDOW, SHORT_WINDOW

logger = logging.getLogger(__name__)

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30


def moving_average(prices: list[float], window: int) -> float:
    """直近window本分の単純移動平均を計算する"""
    if len(prices) < window:
        return None
    return sum(prices[-window:]) / window


def calculate_rsi(prices: list[float], period: int = RSI_PERIOD) -> float:
    """RSIを計算する"""
    if len(prices) < period + 1:
        return None

    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100 if avg_gain > 0 else 50

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def generate_signal(prices: list[float]) -> str:
    """
    価格リストから売買シグナルを判定する。

    移動平均クロス + RSI確度チェック
    """
    if len(prices) < LONG_WINDOW + 1:
        logger.debug("価格データが不足しています")
        return "HOLD"

    short_now = moving_average(prices, SHORT_WINDOW)
    long_now = moving_average(prices, LONG_WINDOW)

    prices_prev = prices[:-1]
    short_prev = moving_average(prices_prev, SHORT_WINDOW)
    long_prev = moving_average(prices_prev, LONG_WINDOW)

    if short_prev is None or long_prev is None:
        logger.debug("移動平均計算不可")
        return "HOLD"

    rsi = calculate_rsi(prices)
    golden_cross = short_prev <= long_prev and short_now > long_now
    dead_cross = short_prev >= long_prev and short_now < long_now

    if golden_cross:
        if rsi is not None and rsi < RSI_OVERBOUGHT:
            logger.info(f"ゴールデンクロス検出: RSI={rsi:.2f}")
            return "BUY"
        logger.debug(f"ゴールデンクロスも強気RSI（{rsi:.2f}）で見送り")
        return "HOLD"

    if dead_cross:
        if rsi is not None and rsi > RSI_OVERSOLD:
            logger.info(f"デッドクロス検出: RSI={rsi:.2f}")
            return "SELL"
        logger.debug(f"デッドクロスも弱気RSI（{rsi:.2f}）で見送り")
        return "HOLD"

    return "HOLD"
