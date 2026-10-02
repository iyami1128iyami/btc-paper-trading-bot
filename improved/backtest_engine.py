"""
バックテスト用エンジン

ライブ運用の PaperTrader とは別に、ファイルへの読み書きを一切行わない
インメモリ版のトレーダーを使ってバックテストを実行する。
これにより、バックテストを何度実行しても本番用の state.json / trade_log.csv を
汚さずに済む。
"""

import math
from dataclasses import dataclass, field


@dataclass
class Trade:
    action: str          # "BUY" or "SELL"
    index: int           # 何本目のローソク足での約定か
    price: float
    amount: float
    reason: str = ""
    pnl: float | None = None   # SELL時のみ: この取引の損益(USDT)


@dataclass
class BacktestResult:
    initial_balance: float
    final_balance: float
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)  # 各時点での評価資産

    @property
    def total_return_pct(self) -> float:
        if self.initial_balance == 0:
            return 0.0
        return (self.final_balance - self.initial_balance) / self.initial_balance * 100

    @property
    def num_round_trips(self) -> int:
        return sum(1 for t in self.trades if t.action == "SELL")

    @property
    def win_rate_pct(self) -> float:
        sells = [t for t in self.trades if t.action == "SELL" and t.pnl is not None]
        if not sells:
            return 0.0
        wins = sum(1 for t in sells if t.pnl > 0)
        return wins / len(sells) * 100

    @property
    def max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        max_dd = 0.0
        for value in self.equity_curve:
            peak = max(peak, value)
            if peak > 0:
                dd = (peak - value) / peak * 100
                max_dd = max(max_dd, dd)
        return max_dd

    @property
    def average_pnl(self) -> float:
        sells = [t.pnl for t in self.trades if t.action == "SELL" and t.pnl is not None]
        if not sells:
            return 0.0
        return sum(sells) / len(sells)

    def summary_text(self) -> str:
        lines = [
            "===== バックテスト結果 =====",
            f"初期資金:        {self.initial_balance:,.2f} USDT",
            f"最終評価資産:    {self.final_balance:,.2f} USDT",
            f"トータルリターン: {self.total_return_pct:+.2f}%",
            f"取引回数(決済):  {self.num_round_trips} 回",
            f"勝率:            {self.win_rate_pct:.1f}%",
            f"平均損益/取引:   {self.average_pnl:+.2f} USDT",
            f"最大ドローダウン: {self.max_drawdown_pct:.2f}%",
        ]
        return "\n".join(lines)


class BacktestTrader:
    """
    ファイルI/Oを行わない、バックテスト専用のシンプルな売買シミュレーター。
    improved.paper_trader.PaperTrader と同じロジック(ストップロス/テイクプロフィット)を
    インメモリで再現する。
    """

    def __init__(
        self,
        initial_balance: float,
        trade_ratio: float,
        stop_loss_pct: float = 5.0,
        take_profit_pct: float = 10.0,
    ):
        if not 0 < trade_ratio <= 1:
            raise ValueError("trade_ratio must be between 0 and 1")
        self.usdt_balance = initial_balance
        self.asset_balance = 0.0
        self.position = "NONE"
        self.entry_price = None
        self.trade_ratio = trade_ratio
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.trades: list[Trade] = []

    def step(self, signal: str, price: float, index: int):
        if self.position == "LONG" and self.entry_price is not None:
            change = (price - self.entry_price) / self.entry_price * 100
            if change >= self.take_profit_pct:
                self._sell(price, index, f"TP {change:.2f}%")
                return
            if change <= -self.stop_loss_pct:
                self._sell(price, index, f"SL {change:.2f}%")
                return

        if signal == "BUY" and self.position == "NONE":
            self._buy(price, index, "Signal")
        elif signal == "SELL" and self.position == "LONG":
            self._sell(price, index, "Signal")

    def _buy(self, price, index, reason):
        spend = self.usdt_balance * self.trade_ratio
        amount = spend / price
        self.usdt_balance -= spend
        self.asset_balance += amount
        self.position = "LONG"
        self.entry_price = price
        self.trades.append(Trade("BUY", index, price, amount, reason))

    def _sell(self, price, index, reason):
        amount = self.asset_balance
        gain = amount * price
        cost = amount * self.entry_price
        pnl = gain - cost
        self.usdt_balance += gain
        self.asset_balance = 0.0
        self.position = "NONE"
        self.entry_price = None
        self.trades.append(Trade("SELL", index, price, amount, reason, pnl=pnl))

    def portfolio_value(self, price: float) -> float:
        return self.usdt_balance + self.asset_balance * price


def run_backtest(
    prices: list[float],
    generate_signal_fn,
    long_window: int,
    initial_balance: float = 1000.0,
    trade_ratio: float = 0.95,
    stop_loss_pct: float = 5.0,
    take_profit_pct: float = 10.0,
) -> BacktestResult:
    """
    過去の価格リストに対して戦略を1本ずつ適用し、バックテストを行う。
    未来のデータを使わないよう、各時点では「その時点までの価格」だけを
    generate_signal_fn に渡す。

    Args:
        prices: 古い順の終値リスト
        generate_signal_fn: strategy.generate_signal と同じシグネチャの関数
        long_window: シグナル判定に必要な最低本数(ウォームアップ期間)
    """
    if len(prices) < long_window + 2:
        raise ValueError("バックテストに十分な価格データがありません")

    trader = BacktestTrader(initial_balance, trade_ratio, stop_loss_pct, take_profit_pct)
    equity_curve = []

    for i in range(long_window + 1, len(prices) + 1):
        window = prices[:i]
        current_price = window[-1]
        signal = generate_signal_fn(window)
        trader.step(signal, current_price, i - 1)
        equity_curve.append(trader.portfolio_value(current_price))

    # バックテスト終了時点でまだポジションを持っていたら、最終価格で清算して評価する
    final_price = prices[-1]
    final_balance = trader.portfolio_value(final_price)

    return BacktestResult(
        initial_balance=initial_balance,
        final_balance=final_balance,
        trades=trader.trades,
        equity_curve=equity_curve,
    )
