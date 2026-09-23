"""移動平均線クロス + RSI 戦略。"""

import logging
import math

from config import LONG_WINDOW, SHORT_WINDOW

logger = logging.getLogger(__name__)

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30


def moving_average(prices: list[float], window: int) -> float | None:
    if window <= 0 or len(prices) < window:
        return None
    return sum(prices[-window:]) / window


def calculate_rsi(prices: list[float], period: int = RSI_PERIOD) -> float | None:
    if period <= 0 or len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    recent = deltas[-period:]
    gains = [max(delta, 0) for delta in recent]
    losses = [max(-delta, 0) for delta in recent]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def generate_signal(prices: list[float]) -> str:
    if len(prices) < LONG_WINDOW + 1:
        return "HOLD"
    if any(not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0 for price in prices):
        logger.warning("不正な価格データを検出したためシグナルを生成しません")
        return "HOLD"

    short_now = moving_average(prices, SHORT_WINDOW)
    long_now = moving_average(prices, LONG_WINDOW)
    short_prev = moving_average(prices[:-1], SHORT_WINDOW)
    long_prev = moving_average(prices[:-1], LONG_WINDOW)
    if None in (short_now, long_now, short_prev, long_prev):
        return "HOLD"

    rsi = calculate_rsi(prices)
    golden_cross = short_prev <= long_prev and short_now > long_now
    dead_cross = short_prev >= long_prev and short_now < long_now

    if golden_cross and rsi is not None and rsi < RSI_OVERBOUGHT:
        return "BUY"
    if dead_cross and rsi is not None and rsi > RSI_OVERSOLD:
        return "SELL"
    return "HOLD"
