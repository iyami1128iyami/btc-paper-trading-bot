# BTCペーパートレードボット

移動平均線クロス戦略(+RSIフィルター、ストップロス/テイクプロフィット)を使用した
自動売買シミュレーター。Binanceの公開APIを使用して価格データを取得します。

## 機能

### ローカル実行(ペーパートレード)
```bash
python main.py
```

### Webサーバー起動(Render等へのデプロイ用)
```bash
python app.py
```

### バックテスト
過去データに対して現在の戦略をそのまま当てはめ、成績を検証できます。

```bash
# デフォルト設定(直近1000本)でバックテスト
python backtest.py

# 本数・時間足・戦略パラメータを指定
python backtest.py --candles 2000 --interval 1h --short 5 --long 25 --stop-loss 3 --take-profit 8
```

実行すると以下のようなサマリーが表示され、取引明細が `backtest_trades.csv` に保存されます。

```
===== バックテスト結果 =====
初期資金:        1,000.00 USDT
最終評価資産:    1,120.50 USDT
トータルリターン: +12.05%
取引回数(決済):  18 回
勝率:            55.6%
平均損益/取引:   +6.69 USDT
最大ドローダウン: 8.32%
```

**オプション一覧**

| オプション | 説明 | デフォルト |
|---|---|---|
| `--symbol` | 対象銘柄 | `config.py`の`SYMBOL` |
| `--interval` | ローソク足間隔(1m, 5m, 1h, 1dなど) | `config.py`の`INTERVAL` |
| `--candles` | 取得するローソク足の本数 | 1000 |
| `--short` | 短期移動平均の期間 | `config.py`の`SHORT_WINDOW` |
| `--long` | 長期移動平均の期間 | `config.py`の`LONG_WINDOW` |
| `--initial-balance` | 初期資金(USDT) | `config.py`の`INITIAL_BALANCE_USDT` |
| `--trade-ratio` | 1回の売買で使う資金の割合 | `config.py`の`TRADE_RATIO` |
| `--stop-loss` | ストップロス(%) | 5.0 |
| `--take-profit` | テイクプロフィット(%) | 10.0 |
| `--out` | 取引明細の出力先CSV | `backtest_trades.csv` |

**注意**: バックテストは本番用の`state.json`/`trade_log.csv`には一切触れません
(専用のインメモリエンジンで計算するため、ペーパートレードの状態を汚しません)。
また、過去データでの好成績が将来の成績を保証するものではありません。

## エンドポイント

- `GET /` - サービス状況確認
- `GET /status` - 最新の売買情報を取得
- `GET /state` - 現在のポートフォリオ状況を取得
- `GET /ping` - ヘルスチェック(UptimeRobot用)

## 設定

`.env.example`を参考に、`.env`ファイルを作成して設定値をカスタマイズしてください。

### 主要な設定
- `SYMBOL`: 取得対象(デフォルト: BTCUSDT)
- `INTERVAL`: ローソク足(デフォルト: 5m)
- `FETCH_INTERVAL_SEC`: API呼び出し間隔(秒)
- `INITIAL_BALANCE_USDT`: 初期資金
- `TRADE_RATIO`: 1回の売買で使用する残高の割合
- `SHORT_WINDOW`: 短期移動平均の期間
- `LONG_WINDOW`: 長期移動平均の期間

## 取引ペアの切り替え

`SYMBOL`環境変数を変更するだけで、別の通貨ペアに切り替えられます(例: `ETHUSDT`, `SOLUSDT`など、Binanceに存在するペアであれば対応可能)。

```
SYMBOL=ETHUSDT
```

**切り替え時の自動保護**: `STATE_FILE`/`LOG_FILE`を環境変数で指定していない場合、
ペアごとに自動でファイル名が分かれます(`state_BTCUSDT.json`, `state_ETHUSDT.json`など)。
これにより、別ペアの残高が混ざることはありません。

もし`STATE_FILE`を固定の名前で指定していて、かつ以前と違うペアに切り替えた場合は、
起動時に**エラーで停止**します(別ペアの残高を誤って引き継いで計算が狂うのを防ぐ安全装置です)。
意図的な切り替えであれば、その状態ファイルを削除してから起動し直してください。

Renderでペアを切り替える場合は、ダッシュボードの Environment タブで`SYMBOL`の値を変更し、
サービスを再起動(Manual Deploy → Clear build cache & deploy、または単に再起動)してください。

## ログ出力

売買履歴は`trade_log_<SYMBOL>.csv`に記録されます:

```
timestamp,symbol,action,price,amount,usdt_balance,asset_balance,reason
2024-01-01T12:00:00+00:00,BTCUSDT,BUY,45000.00,0.021111,50.00,0.021111,Signal
2024-01-01T12:05:00+00:00,BTCUSDT,SELL,45100.00,0.021111,1002.11,0.00,Signal
```

## アーキテクチャ

```
config.py                      設定管理
main.py                        ローカル実行用メインループ
app.py                         Flask Webサーバー(クラウドデプロイ用)
backtest.py                    バックテストCLI
improved/
  ├── data_fetcher.py          Binance APIを使用した価格取得・過去データ取得
  ├── strategy.py               移動平均線クロス + RSI戦略
  ├── paper_trader.py          ペーパートレード実行エンジン(本番用、ファイル永続化あり)
  └── backtest_engine.py       バックテスト専用エンジン(インメモリ、ファイルに触れない)
```

## 注意

これを実際の取引所で使用する前に、十分な期間・複数の相場環境でバックテストを行い、
戦略を検証してください。これは教育目的のプロジェクトです。実際の資金を使う場合は
自己責任で、リスク管理を行ったうえで運用してください。
