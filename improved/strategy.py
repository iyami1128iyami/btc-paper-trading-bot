"""移動平均線クロス + RSI + ADX(トレンド強度フィルター)戦略。"""

import logging
import math

from config import ADX_PERIOD, ADX_THRESHOLD, LONG_WINDOW, RSI_PERIOD, SHORT_WINDOW

logger = logging.getLogger(__name__)

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


def calculate_adx(prices: list[float], period: int = ADX_PERIOD) -> float | None:
    """
    終値のみを使った簡易版ADX(平均方向性指数)。

    本来のADXは高値・安値・終値(OHLC)から「True Range」を計算するが、
    現在のデータソース(CoinGecko)は終値のみを提供しているため、
    終値の変化幅を代わりに使う近似値として計算する
    (ローソク足の実際の値幅より情報量は少ないが、
    「方向性のある動きかどうか」の大まかな目安としては機能する)。

    値が高いほど(目安: 25以上)はっきりしたトレンドが出ている可能性が高く、
    低いほど(目安: 20未満)方向感のないレンジ相場の可能性が高い。
    """
    if period <= 0:
        return None
    # ワイルダーの平滑化を一度安定させるには、最初のperiod本の初期化に加えて
    # DXをperiod本分積み上げる必要があるため、合計で2*period+1本の価格が要る。
    if len(prices) < period * 2 + 1:
        return None

    plus_dm = []
    minus_dm = []
    tr = []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        plus_dm.append(max(change, 0.0))
        minus_dm.append(max(-change, 0.0))
        tr.append(abs(change))

    def wilder_smooth(values):
        smoothed = [sum(values[:period])]
        for v in values[period:]:
            smoothed.append(smoothed[-1] - smoothed[-1] / period + v)
        return smoothed

    smoothed_tr = wilder_smooth(tr)
    smoothed_plus_dm = wilder_smooth(plus_dm)
    smoothed_minus_dm = wilder_smooth(minus_dm)

    dx_values = []
    for s_tr, s_pdm, s_mdm in zip(smoothed_tr, smoothed_plus_dm, smoothed_minus_dm):
        if s_tr == 0:
            dx_values.append(0.0)
            continue
        plus_di = 100 * s_pdm / s_tr
        minus_di = 100 * s_mdm / s_tr
        di_sum = plus_di + minus_di
        dx_values.append(100 * abs(plus_di - minus_di) / di_sum if di_sum else 0.0)

    if len(dx_values) < period:
        return None

    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def generate_signal(prices: list[float]) -> str:
    """
    レジームスイッチング方式の戦略。

    ADXで「トレンド相場」か「レンジ相場」かを判定し、それぞれに合ったロジックで
    シグナルを出す。移動平均線クロスはトレンドに乗る手法のため、レンジ相場(方向感のない
    往復相場)では機能しにくい。その代わりレンジ相場では、RSIを使った逆張り(平均回帰)に
    切り替える。

    - トレンド相場(ADX >= ADX_THRESHOLD、またはADXがまだ計算できない場合):
      従来通りの移動平均線クロス + RSIフィルター
    - レンジ相場(ADX < ADX_THRESHOLD):
      RSIが売られすぎ(<=30)ならBUY、買われすぎ(>=70)ならSELLの逆張り
    """
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
    adx = calculate_adx(prices)
    is_ranging = adx is not None and adx < ADX_THRESHOLD

    if is_ranging:
        if rsi is None:
            return "HOLD"
        if rsi <= RSI_OVERSOLD:
            logger.info("レンジ相場(ADX=%.1f)と判断。RSI逆張りでBUYシグナル(RSI=%.1f)", adx, rsi)
            return "BUY"
        if rsi >= RSI_OVERBOUGHT:
            logger.info("レンジ相場(ADX=%.1f)と判断。RSI逆張りでSELLシグナル(RSI=%.1f)", adx, rsi)
            return "SELL"
        return "HOLD"

    # トレンド相場(またはADX判定不能時のデフォルト): 移動平均線クロス + RSIフィルター
    golden_cross = short_prev <= long_prev and short_now > long_now
    dead_cross = short_prev >= long_prev and short_now < long_now
    if golden_cross and rsi is not None and rsi < RSI_OVERBOUGHT:
        return "BUY"
    if dead_cross and rsi is not None and rsi > RSI_OVERSOLD:
        return "SELL"
    return "HOLD"
