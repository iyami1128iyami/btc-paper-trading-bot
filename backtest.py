"""
バックテスト実行スクリプト

Binanceの公開APIから過去データを取得し、現在の戦略(移動平均クロス+RSI)を
その期間にそのまま当てはめて成績を検証する。

使い方:
    python backtest.py
    python backtest.py --candles 1500 --interval 1h
    python backtest.py --short 5 --long 25 --stop-loss 3 --take-profit 8
"""

import argparse
import csv

import config
from improved.data_fetcher import fetch_historical_closes
from improved.strategy import generate_signal
from improved.backtest_engine import run_backtest


def parse_args():
    parser = argparse.ArgumentParser(description="移動平均クロス+RSI戦略のバックテスト")
    parser.add_argument("--symbol", default=config.SYMBOL, help="対象銘柄 (例: BTCUSDT)")
    parser.add_argument("--interval", default=config.INTERVAL, help="ローソク足間隔 (例: 5m, 1h, 1d)")
    parser.add_argument("--candles", type=int, default=1000, help="取得するローソク足の本数")
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

    # 戦略パラメータはconfigモジュールの値を直接書き換えて反映する
    # (strategy.generate_signal が config.SHORT_WINDOW / LONG_WINDOW を参照しているため)
    config.SYMBOL = args.symbol
    config.INTERVAL = args.interval
    config.SHORT_WINDOW = args.short
    config.LONG_WINDOW = args.long

    print(f"過去データを取得中... (銘柄={args.symbol}, 間隔={args.interval}, 本数={args.candles})")
    prices = fetch_historical_closes(total=args.candles)
    print(f"取得完了: {len(prices)} 本の終値データ")

    result = run_backtest(
        prices=prices,
        generate_signal_fn=generate_signal,
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
