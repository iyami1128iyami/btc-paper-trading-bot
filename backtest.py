"""
バックテスト実行スクリプト

CoinGeckoの公開APIから過去データを取得し、現在の戦略(移動平均クロス+RSI)を
その期間にそのまま当てはめて成績を検証する。

使い方:
    python backtest.py
    python backtest.py --candles 1500
    python backtest.py --short 5 --long 25 --stop-loss 3 --take-profit 8

注意: CoinGeckoはローソク足間隔を直接指定できず、取得期間(日数)に応じて
自動で粒度(何分/時間おきか)が決まる。--candlesの本数からおおよその
期間を逆算して取得する(improved/data_fetcher.fetch_historical_closes参照)。
"""

import argparse
import csv

import config
import improved.data_fetcher as data_fetcher
import improved.strategy as strategy
from improved.backtest_engine import run_backtest


def parse_args():
    parser = argparse.ArgumentParser(description="移動平均クロス+RSI戦略のバックテスト")
    parser.add_argument("--symbol", default=config.SYMBOL, help="対象銘柄 (例: BTCUSDT)")
    parser.add_argument("--candles", type=int, default=1000, help="取得する価格データの本数(目安)")
    parser.add_argument("--short", type=int, default=config.SHORT_WINDOW, help="短期移動平均の期間")
    parser.add_argument("--long", type=int, default=config.LONG_WINDOW, help="長期移動平均の期間")
    parser.add_argument("--initial-balance", type=float, default=config.INITIAL_BALANCE_USDT)
    parser.add_argument("--trade-ratio", type=float, default=config.TRADE_RATIO)
    parser.add_argument("--stop-loss", type=float, default=5.0, help="ストップロス(%)")
    parser.add_argument("--take-profit", type=float, default=10.0, help="テイクプロフィット(%)")
    parser.add_argument("--out", default="backtest_trades.csv", help="取引明細の出力先CSV")
    return parser.parse_args()


def main():
    args = parse_args()

    # 戦略パラメータを反映する。
    # 注意: `from config import SHORT_WINDOW` のようにimportした側(strategy.py /
    # data_fetcher.py)は、import時点の値を自分のモジュール名前空間にコピーして
    # 持っているため、config.SHORT_WINDOW をここで書き換えただけでは反映されない
    # (Pythonの名前束縛の仕様)。そのため、既にimport済みのモジュール自身の
    # 属性も直接書き換える必要がある。
    config.SYMBOL = args.symbol
    config.SHORT_WINDOW = args.short
    config.LONG_WINDOW = args.long
    data_fetcher.SYMBOL = args.symbol
    strategy.SHORT_WINDOW = args.short
    strategy.LONG_WINDOW = args.long

    print(f"過去データを取得中... (銘柄={args.symbol}, 本数={args.candles})")
    prices = data_fetcher.fetch_historical_closes(total=args.candles)
    print(f"取得完了: {len(prices)} 本の終値データ")

    result = run_backtest(
        prices=prices,
        generate_signal_fn=strategy.generate_signal,
        long_window=args.long,
        initial_balance=args.initial_balance,
        trade_ratio=args.trade_ratio,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
    )

    print()
    print(result.summary_text())

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["candle_index", "action", "price", "amount", "reason", "pnl_usdt"])
        for t in result.trades:
            writer.writerow(
                [t.index, t.action, round(t.price, 2), round(t.amount, 8), t.reason,
                 round(t.pnl, 2) if t.pnl is not None else ""]
            )
    print(f"\n取引明細を {args.out} に保存しました")


if __name__ == "__main__":
    main()
