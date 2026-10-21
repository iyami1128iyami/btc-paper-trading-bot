# BTCペーパートレードボット

ADXによる**レジームスイッチング戦略**(ストップロス/テイクプロフィット付き)を使用した
自動売買シミュレーター。**CoinGecko**の公開APIを使用して価格データを取得します
(地域制限がなく、Render等どのリージョンからでもアクセス可能)。

**レジームスイッチングについて**: ADX(平均方向性指数)で相場の種類を判定し、
それぞれに合ったロジックに自動で切り替えます。

- **トレンド相場**(ADX >= 閾値、デフォルト25): 移動平均線クロス + RSIフィルター
  (順張り。トレンドに乗る手法)
- **レンジ相場**(ADX < 閾値): RSI逆張り(平均回帰)。RSIが売られすぎ(<=30)でBUY、
  買われすぎ(>=70)でSELL(往復相場で反発を狙う手法)

移動平均線クロスはトレンド相場向けの手法でレンジ相場では機能しにくいため、
相場の性質に応じて手法そのものを切り替えることで対応しています。
ただし現在のデータソースは終値のみのため、本来の(高値・安値を使う)ADXではなく、
終値ベースの近似値で計算しています(`improved/strategy.py`の`calculate_adx`参照)。

**トレーリングストップについて**: 含み益が`TRAILING_STOP_ACTIVATION_PCT`(デフォルト1.0%)に
達すると有効化され、以降は保有中の最高値から`TRAILING_STOP_PCT`(デフォルト0.5%)下落した時点で
自動決済します。伸びているトレンドにはそのまま乗り続けつつ、反転したら早めに利益を確定させる狙いです。
テイクプロフィット(固定10%)・ストップロス(固定-5%)とは独立して動作し、どれか条件を満たした時点で決済されます。

**シグナル確認(ダマシ対策)について**: クロスした瞬間・RSIが閾値を超えた瞬間に
即座にエントリーするのではなく、`CONFIRMATION_BARS`(デフォルト2)本連続で
条件が継続して初めてエントリーします。1本だけの一時的なノイズでクロスし、
すぐ戻ってしまう「ダマシ」によるエントリーを減らす狙いです。

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

## 複数プロセスでの二重取引防止

`REDIS_URL`を設定している場合、取引ループは実行前にRedis上のリーダーロックを確認します。
これにより、万一gunicornのワーカー数が2以上になるような構成に変わっても、
実際に取引を行うのは1プロセスだけに保たれ、二重発注や状態競合を防ぎます
(`improved/leader_lock.py`)。`REDIS_URL`未設定時はこの保護は働かないため、
単一プロセスでの運用を前提としてください(現状の`WEB_CONCURRENCY=1`設定と合わせて、
二重の安全策になっています)。

## 状態の永続化(Render Key Value / Redis)

Renderは再デプロイのたびにディスクを初期化するため、何も設定しないと
**コードを変更して再デプロイするたびにポジション・残高がリセットされます**。

これを防ぐには、Render Key Value(Redis互換、無料プランあり)を使って、
状態(`state.json`相当の内容)をデプロイをまたいで保持できるようにします。

**設定方法**
1. Renderダッシュボードで「New +」→「Key Value」を作成(無料プランでOK)
2. 作成したKey Valueの「Connect」タブで **Internal Connection String** を確認
3. そのURL(`rediss://...`)を環境変数 `REDIS_URL` に設定

`REDIS_URL`を設定しない場合はローカルファイルにフォールバックしますが、
前述の通りデプロイのたびに消えるため、**本運用では設定を強く推奨**します。

**注意**: Render無料プランのKey Valueはディスクへの永続化(snapshot)に対応していないため、
Key Valueインスタンス自体が再起動された場合はデータが失われます。ただし、
これはアプリのコードをデプロイし直すのとは別物で、通常の開発中の再デプロイでは
Key Value側は再起動されないため、今回の「デプロイのたびに消える」問題は解消されます。

## デモトレード(Binance Testnet)

`TRADING_MODE=demo`を設定すると、内部シミュレーション(ペーパートレード)の代わりに、
**Binance Testnet(仮想資金、実際にAPIで注文を出す)に接続して取引します**。
売買ロジック(移動平均クロス/RSI逆張り/ストップロス/テイクプロフィット/
トレーリングストップ/最低利益チェック)はペーパートレードと同じものを使います。

**設定方法**
1. [https://testnet.binance.vision](https://testnet.binance.vision) でGitHubアカウントを使ってログイン
2. 「Generate HMAC_SHA256 Key」でAPIキー・Secret Keyを発行
3. 環境変数を設定:
   - `TRADING_MODE=demo`
   - `BINANCE_API_KEY=<発行されたAPIキー>`
   - `BINANCE_API_SECRET=<発行されたSecret Key>`

`BINANCE_BASE_URL`(デフォルト: `https://testnet.binance.vision`)を本番用のURLに変更すれば、
将来的に同じコードで本番環境にも接続できます(Binanceは本番・テストネットでAPI形式がほぼ共通)。

**仕組み**
- 「いくら使うか」の計算は、**取引所の実際の口座残高ではなく`DEMO_CAPITAL_USDT`
  (デフォルト100、自分の持ち金のつもり)を基準にした自前の帳簿**で行う
  (`usdt_balance` / `asset_balance`としてRedis/ファイルに保存)。
  理由は2つ: ① Binance Testnetの口座にはテスト用の多額の資金が自動付与されるため、
  実際の残高をそのまま使うと本番の金額感と懸け離れてしまう。② 決済時に「実際の口座残高を
  丸ごと売る」実装だと、テスト用に最初から口座にあった資産まで一緒に売ってしまう
  (実際に発生した不具合)。自分が買った分だけを自前で記録し、それだけを売ることで防ぐ
- 実際の注文(発注・約定)はBinance APIを通して本物を出すため、約定価格のズレはそのまま
  自前の帳簿に反映される。実際の口座残高への問い合わせは「自前の帳簿が実際の残高を
  超えていないか」の安全確認にのみ使う(超えていれば自動的に減額して発注する)
- 買い注文は`quoteOrderQty`(使う金額)で指定し、取引所側に数量計算を任せる
- 売り注文は、取引所の最小注文単位(LOT_SIZE)に合わせて数量を丸めてから発注する
- 残高が取引所の最小注文金額(MIN_NOTIONAL)に満たない場合は、発注せず見送る

**注意**: `demo`モードは実際にBinance(テストネット)のAPIへ注文を送信します。仮想資金なので
金銭的リスクはありませんが、`paper`モード(完全な内部シミュレーション)とは動作が異なる点
(APIのレート制限、約定価格のズレ、注文拒否など)に留意してください。

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
- `ADX_PERIOD`: ADX(トレンド強度)の計算期間(デフォルト: 14)
- `ADX_THRESHOLD`: この値未満はレンジ相場とみなし取引を見送る(デフォルト: 25.0)
- `CONFIRMATION_BARS`: シグナルが継続しているか確認する本数(デフォルト: 2。ダマシ対策)
- `MIN_PROFIT_PCT_TO_EXIT`: 通常シグナルでの決済に必要な最低利益%(デフォルト: 0.3。
  往復手数料で負けないための下限。ストップロス/テイクプロフィット/トレーリングストップは
  これと独立して発動する)
- `TRAILING_STOP_ACTIVATION_PCT`: トレーリングストップが有効化される含み益%(デフォルト: 1.0)
- `TRAILING_STOP_PCT`: 有効化後、最高値からこの%下落したら決済(デフォルト: 0.5)
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`: Telegram通知を使う場合に設定(任意、上記「通知」セクション参照)
- `REDIS_URL`: 状態の永続化にRender Key Valueを使う場合に設定(上記「状態の永続化」セクション参照、本運用では推奨)
- `TRADING_MODE`: `paper`(デフォルト、内部シミュレーション)または`demo`(Binance Testnetに実際に発注)
- `BINANCE_API_KEY` / `BINANCE_API_SECRET`: `TRADING_MODE=demo`の場合に必須
- `BINANCE_BASE_URL`: Binance APIのベースURL(デフォルト: Testnet。本番移行時に変更)
- `DEMO_CAPITAL_USDT`: デモトレードで「自分の持ち金」とみなす仮想資金(デフォルト: 100)

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
  ├── strategy.py              レジームスイッチング(移動平均クロス + RSI逆張り)戦略
  ├── paper_trader.py          ペーパートレード実行エンジン(内部シミュレーション、Redis/ファイル永続化)
  ├── binance_client.py        Binance(Testnet/本番)の認証付きAPIクライアント
  ├── demo_trader.py           デモトレード実行エンジン(Binance Testnetに実際に発注)
  ├── backtest_engine.py       バックテスト専用エンジン(インメモリ、ファイルに触れない)
  ├── notifier.py              Telegram通知
  └── leader_lock.py           複数プロセスでの二重取引防止(Redisベースのリーダーロック)
```

## 注意

これを実際の取引所で使用する前に、十分な期間・複数の相場環境でバックテストを行い、
戦略を検証してください。これは教育目的のプロジェクトです。実際の資金を使う場合は
自己責任で、リスク管理を行ったうえで運用してください。
