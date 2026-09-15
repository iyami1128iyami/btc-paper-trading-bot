# デプロイガイド

## 📦 前準備

### ローカルテスト

```bash
# 依存パッケージのインストール
pip install -r requirements.txt

# 環境設定ファイルの作成
cp .env.example .env

# ローカル実行テスト（5分程度）
python main.py
```

---

## ☁️ Renderへのデプロイ

### 1. Renderアカウント作成

- [Render公式](https://render.com) にアクセス
- GitHubアカウントで登録
- 無料プラン選択

### 2. GitHubリポジトリの接続

```
Render Dashboard → New Web Service → Connect Git Repository
→ btc-paper-trading-bot を選択
```

### 3. Web Service設定

| 設定項目 | 値 |
|---------|-----|
| **Name** | btc-paper-trading-bot |
| **Environment** | Python 3.11 |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `gunicorn --bind 0.0.0.0:10000 app:app` |
| **Plan** | Free |

### 4. 環境変数の設定

Render Dashboard → Environment に以下を追加：

```
SYMBOL=BTCUSDT
INTERVAL=5m
FETCH_INTERVAL_SEC=300          # 本番は5分間隔推奨
INITIAL_BALANCE_USDT=1000.0
TRADE_RATIO=0.95
SHORT_WINDOW=5
LONG_WINDOW=20
LOG_FILE=/tmp/trade_log.csv     # 無料プランではディスク永続性なし
STATE_FILE=/tmp/state.json
```

### 5. デプロイ実行

```
Git Push → Render自動ビルド・デプロイ
```

---

## 📋 ヘルスチェック設定

### UptimeRobotの設定

1. [UptimeRobot](https://uptimerobot.com) で無料登録
2. 新規モニター作成
   - **Monitor Type**: HTTP(s)
   - **URL**: `https://your-app.onrender.com/ping`
   - **Monitoring Interval**: 5分
   - **Alerting**: メール通知

### RenderのHealth Check

```bash
# Procfile の確認
web: gunicorn --bind 0.0.0.0:10000 --workers 1 --timeout 120 app:app
```

---

## 🔧 デプロイ後の検証

### ステータス確認

```bash
# サービス稼働確認
curl https://your-app.onrender.com/
# 応答: {"service": "btc-paper-trading-bot", "status": "running"}

# 最新ステータス取得
curl https://your-app.onrender.com/status

# ポートフォリオ状況確認
curl https://your-app.onrender.com/state

# ヘルスチェック
curl https://your-app.onrender.com/ping
# 応答: pong
```

### ログ確認

```
Render Dashboard → Logs タブで実行ログを確認
```

---

## ⚠️ 無料プランの制限

| 制限項目 | 内容 | 対策 |
|---------|------|------|
| **ディスク** | 1GB（再起動で削除） | データベース連携を検討 |
| **スリープ** | 15分無アクセスでスリープ | UptimeRobotで定期ping |
| **メモリ** | 512MB | 軽量なコード最適化 |
| **コンピュート** | 0.5 vCPU | 負荷軽減（データ取得間隔拡大） |

---

## 💾 データ永続化ソリューション

### オプション1: GitHub Actionsでの定期バックアップ

```yaml
# .github/workflows/backup.yml
name: Backup Trading Logs
on:
  schedule:
    - cron: '0 */6 * * *'  # 6時間ごと

jobs:
  backup:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Pull latest logs
        run: |
          curl https://your-app.onrender.com/state > logs/state.json
```

### オプション2: MongoDBの無料ティア利用

```python
# MongoDB接続（将来実装）
from pymongo import MongoClient

client = MongoClient(os.getenv("MONGODB_URI"))
db = client.trading_bot

# トレード履歴を永続化
db.trades.insert_one({"timestamp": datetime.now(), "action": "BUY", ...})
```

---

## 🚨 トラブルシューティング

### デプロイエラー: "ModuleNotFoundError"

```bash
# requirements.txtの確認
cat requirements.txt
# 不足パッケージがあれば追加
pip freeze > requirements.txt
```

### API呼び出し失敗: "Connection timeout"

```python
# timeout値を増やす
resp = requests.get(BASE_URL, params=params, timeout=30)  # デフォルト10秒
```

### ボットが停止する: "Memory exceeded"

```python
# データ取得間隔を拡大
FETCH_INTERVAL_SEC=600  # 10分
```

---

## 📞 サポート

- **Renderドキュメント**: https://render.com/docs
- **Pythonドキュメント**: https://docs.python.org
- **GitHubコミュニティ**: このリポジトリのIssues
