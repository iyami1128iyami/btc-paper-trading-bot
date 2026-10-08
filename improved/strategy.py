"""移動平均線クロス + RSI + ADX(トレンド強度フィルター)戦略。"""

import logging
import math

from config import ADX_PERIOD, ADX_THRESHOLD, CONFIRMATION_BARS, LONG_WINDOW, RSI_PERIOD, SHORT_WINDOW

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


def _ma_relation_series(prices: list[float], lookback: int) -> list[bool] | None:
    """
    直近 lookback+1 時点における「短期MA > 長期MA か」の真偽値を古い順で返す。
    途中でどれか計算できない(データ不足)場合はNoneを返す。
    """
    relation = []
    for back in range(lookback, -1, -1):
        window = prices if back == 0 else prices[:-back]
        short = moving_average(window, SHORT_WINDOW)
        long_ = moving_average(window, LONG_WINDOW)
        if short is None or long_ is None:
            return None
        relation.append(short > long_)
    return relation


def _trailing_values(compute_fn, prices: list[float], lookback: int) -> list[float] | None:
    """
    直近 lookback+1 時点について、compute_fn(その時点までの価格列)の結果を
    古い順のリストで返す。どれか1つでも計算できなければNoneを返す。
    """
    values = []
    for back in range(lookback, -1, -1):
        window = prices if back == 0 else prices[:-back]
        value = compute_fn(window)
        if value is None:
            return None
        values.append(value)
    return values


def generate_signal(prices: list[float]) -> str:
    """
    レジームスイッチング + シグナル確認(ダマシ対策)方式の戦略。

    ADXで「トレンド相場」か「レンジ相場」かを判定し、それぞれに合ったロジックで
    シグナルを出す。移動平均線クロスはトレンドに乗る手法のため、レンジ相場(方向感のない
    往復相場)では機能しにくい。その代わりレンジ相場では、RSIを使った逆張り(平均回帰)に
    切り替える。

    - トレンド相場(ADX >= ADX_THRESHOLD、またはADXがまだ計算できない場合):
      移動平均線クロス + RSIフィルター
    - レンジ相場(ADX < ADX_THRESHOLD):
      RSIが売られすぎ(<=30)ならBUY、買われすぎ(>=70)ならSELLの逆張り

    シグナル確認(CONFIRMATION_BARS): クロスした瞬間・閾値を超えた瞬間に
    即座にシグナルを出すのではなく、CONFIRMATION_BARS本連続で条件が
    継続して初めてシグナルを出す。1本だけのノイズによる「ダマシ」を減らす狙い。
    """
    min_len = LONG_WINDOW + CONFIRMATION_BARS + 1
    if len(prices) < min_len:
        return "HOLD"
    if any(not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0 for price in prices):
        logger.warning("不正な価格データを検出したためシグナルを生成しません")
        return "HOLD"

    adx = calculate_adx(prices)
    is_ranging = adx is not None and adx < ADX_THRESHOLD

    if is_ranging:
        rsi_series = _trailing_values(calculate_rsi, prices, CONFIRMATION_BARS)
        if rsi_series is None:
            return "HOLD"
        if all(r <= RSI_OVERSOLD for r in rsi_series):
            logger.info(
                "レンジ相場(ADX=%.1f)と判断。RSI逆張りでBUYシグナル(直近%d本RSI<=%.0f, 現在RSI=%.1f)",
                adx, CONFIRMATION_BARS + 1, RSI_OVERSOLD, rsi_series[-1],
            )
            return "BUY"
        if all(r >= RSI_OVERBOUGHT for r in rsi_series):
            logger.info(
                "レンジ相場(ADX=%.1f)と判断。RSI逆張りでSELLシグナル(直近%d本RSI>=%.0f, 現在RSI=%.1f)",
                adx, CONFIRMATION_BARS + 1, RSI_OVERBOUGHT, rsi_series[-1],
            )
            return "SELL"
        return "HOLD"

    # トレンド相場(またはADX判定不能時のデフォルト): 移動平均線クロス + RSIフィルター
    relation = _ma_relation_series(prices, CONFIRMATION_BARS)
    if relation is None:
        return "HOLD"
    # relation[0]が確認期間の直前の状態、relation[1:]が「継続を確認する」直近の期間
    confirmation_window = relation[1:]
    golden_cross_confirmed = all(confirmation_window) and not relation[0]
    dead_cross_confirmed = all(not r for r in confirmation_window) and relation[0]

    rsi = calculate_rsi(prices)
    if golden_cross_confirmed and rsi is not None and rsi < RSI_OVERBOUGHT:
        logger.info("トレンド相場(ADX=%s)。%d本連続確認のうえゴールデンクロスでBUY", adx, CONFIRMATION_BARS)
        return "BUY"
    if dead_cross_confirmed and rsi is not None and rsi > RSI_OVERSOLD:
        logger.info("トレンド相場(ADX=%s)。%d本連続確認のうえデッドクロスでSELL", adx, CONFIRMATION_BARS)
        return "SELL"
    return "HOLD"
