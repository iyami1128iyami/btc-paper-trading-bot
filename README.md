# BTCペーパートレードボット

移動平均線クロス戦略(+RSIフィルター、ストップロス/テイクプロフィット)を使用した
自動売買シミュレーター。**CoinGecko**の公開APIを使用して価格データを取得します
(地域制限がなく、Render等どのリージョンからでもアクセス可能)。

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
# デフォルト設定(直近1000件相当)でバックテスト
python backtest.py

# 本数・戦略パラメータを指定
python backtest.py --candles 2000 --short 5 --long 25 --stop-loss 3 --take-profit 8
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
| `--candles` | 取得する価格データの本数の目安 | 1000 |
| `--short` | 短期移動平均の期間 | `config.py`の`SHORT_WINDOW` |
| `--long` | 長期移動平均の期間 | `config.py`の`LONG_WINDOW` |
| `--initial-balance` | 初期資金(USDT) | `config.py`の`INITIAL_BALANCE_USDT` |
| `--trade-ratio` | 1回の売買で使う資金の割合 | `config.py`の`TRADE_RATIO` |
| `--stop-loss` | ストップロス(%) | 5.0 |
| `--take-profit` | テイクプロフィット(%) | 10.0 |
| `--out` | 取引明細の出力先CSV | `backtest_trades.csv` |

**注意**:
- バックテストは本番用の`state.json`/`trade_log.csv`には一切触れません(専用のインメモリエンジンで計算)。
- CoinGeckoは「何日分」の指定しかできず、期間に応じて粒度(5分/1時間/1日間隔)が自動で変わります。`--candles`の本数から必要な日数を逆算して取得します。
- 過去データでの好成績が将来の成績を保証するものではありません。

## エンドポイント

- `GET /` - サービス状況確認(現在の取引ペアも表示)
- `GET /status` - 最新の売買情報を取得
- `GET /state` - 現在のポートフォリオ状況を取得
- `GET /ping` - ヘルスチェック(UptimeRobot用)

## データソース: CoinGecko

以前はBinanceの公開APIを使用していましたが、**Renderの米国リージョンからのアクセスが
451エラー(法的理由によるブロック)で拒否される問題**があったため、地域制限のない
CoinGeckoに切り替えました。

- APIキー不要(無料プランは5〜30回/分程度のレート制限)
- もっと高頻度に使いたい場合は、CoinGeckoの無料Demo APIキーを取得し、
  `COINGECKO_API_KEY`環境変数に設定するとレート制限が緩和されます(100回/分)
- 対応銘柄は`improved/data_fetcher.py`の`SYMBOL_TO_COINGECKO`に一覧があります
  (BTC, ETH, SOL, BNB, XRP, DOGE, ADAのUSDT/JPYペアなど)
- 一覧にないペアを使いたい場合は、`COINGECKO_ID`と`COINGECKO_VS_CURRENCY`の
  環境変数で直接指定できます(例: `COINGECKO_ID=polkadot`, `COINGECKO_VS_CURRENCY=usd`)

## 通知(Telegram)

ポジションの変化(BUY/SELL)やエラーの発生・復旧をTelegramに通知できます(任意機能)。

**設定方法**
1. Telegramで [@BotFather](https://t.me/BotFather) を開き、`/newbot` でBotを作成(名前を聞かれるので好きに入力)
2. 発行される「トークン」(`123456:ABC-DEF...`のような文字列)をコピー → `TELEGRAM_BOT_TOKEN`
3. 作成したBotとのトーク画面を開き、何か適当なメッセージを送る(例: 「test」)
4. ブラウザで以下にアクセスし、`"chat":{"id":...}` の数値(`chat_id`)を確認する

   ```
   https://api.telegram.org/bot<取得したトークン>/getUpdates
   ```

5. その数値を `TELEGRAM_CHAT_ID` に設定

環境変数 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` のどちらかが未設定の場合、通知機能は何もせず静かに無効化されます(エラーにはなりません)。

**通知されるタイミング**
- 💰 BUY/SELLが成立した時(価格・数量・損益を含む)
- 🚨 価格取得や取引処理でエラーが発生した時(連続エラー中は最初の1回だけ通知し、スパムを防止)
- ✅ エラーから復旧した時
- 🚨 状態ファイル(`state.json`)が破損していて初期状態にリセットした時
- 🔥 起動そのものに失敗した時

## 設定

`.env.example`を参考に、`.env`ファイルを作成して設定値をカスタマイズしてください。

### 主要な設定
- `SYMBOL`: 取得対象(デフォルト: BTCUSDT)
- `FETCH_INTERVAL_SEC`: API呼び出し間隔(秒)
- `INITIAL_BALANCE_USDT`: 初期資金
- `TRADE_RATIO`: 1回の売買で使用する残高の割合
- `SHORT_WINDOW`: 短期移動平均の期間
- `LONG_WINDOW`: 長期移動平均の期間
- `COINGECKO_ID` / `COINGECKO_VS_CURRENCY`: マッピング表にない銘柄を使う場合に指定
- `COINGECKO_API_KEY`: レート制限を緩和したい場合(任意)
- `RSI_PERIOD`: RSIの計算期間(デフォルト: 14。LONG_WINDOW未満にすると警告が出ます)
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`: Telegram通知を使う場合に設定(任意、上記「通知」セクション参照)

## 取引ペアの切り替え

`SYMBOL`環境変数を変更するだけで、別の通貨ペアに切り替えられます
(例: `ETHUSDT`, `SOLUSDT`など、CoinGeckoのマッピング表にあるペア)。

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
  ├── data_fetcher.py          CoinGecko APIを使用した価格取得・過去データ取得
  ├── strategy.py               移動平均線クロス + RSI戦略
  ├── paper_trader.py          ペーパートレード実行エンジン(本番用、ファイル永続化あり)
  └── backtest_engine.py       バックテスト専用エンジン(インメモリ、ファイルに触れない)
```

## 注意

これを実際の取引所で使用する前に、十分な期間・複数の相場環境でバックテストを行い、
戦略を検証してください。これは教育目的のプロジェクトです。実際の資金を使う場合は
自己責任で、リスク管理を行ったうえで運用してください。
