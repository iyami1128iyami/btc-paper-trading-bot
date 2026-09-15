"""
BTCペーパートレードボット

移動平均線クロス戦略を使用した自動売買シミュレーター。
BinanceのAPIを使用して価格データを取得し、
移動平均線のクロスを検出して売買を実行します。

## 機能

### ローカル実行
```bash
python main.py
```

### Webサーバー起動(開発機用)
```bash
python app.py
```

### Renderデプロイ
`Procfile`を使用してRenderにデプロイ。

## エンドポイント

- `GET /` - サービス状況確認
- `GET /status` - 最新の売買情報を取得
- `GET /state` - 現在のポートフォリオ統計を取得
- `GET /ping` - ヘルスチェック(ウプタイムロボット用)

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

## ログ出力

売買业務は`trade_log.csv`に記録されます:

```
timestamp,action,price,amount_btc,usdt_balance,btc_balance
2024-01-01T12:00:00+00:00,BUY,45000.00,0.021111,950.00,0.021111
2024-01-01T12:05:00+00:00,SELL,45100.00,0.021111,1000.00,0.00
```

## アーキテクチャ

- `config.py` - 設定管理
- `data_fetcher.py` - Binance APIを使用した価格取得
- `strategy.py` - 移動平均線クロス戦略
- `paper_trader.py` - ペーパートレード実行エンジン
- `main.py` - ローカル実行用メインループ
- `app.py` - Flask Webサーバー(クラウドデプロイ用)

## 注意

これを実際の取引所で使用する前に、評価ストラテジーを十分に検証してください。
これは教育的目的で選機、リスク管理を会社で実行して下さい。
