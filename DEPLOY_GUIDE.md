# カイポケ自動化 API - VPSデプロイガイド

## 概要

このAPIサーバーは、Google Apps Script (GAS) からHTTPリクエストを受け取り、
Playwrightでカイポケの操作を自動化します。

## 必要なファイル

```
PlaywrightTest1/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── api_server.py
├── state.json          # カイポケのセッション情報
├── lib/
│   ├── common.py       # 共通関数
│   ├── diff_engine.py  # 差分比較エンジン
│   └── google_drive.py # Drive連携（オプション）
├── commands/
│   ├── expand.py       # 月間スケジュール展開
│   ├── export.py       # CSV出力
│   └── auto_apply.py   # 差分適用
└── data/               # CSVデータ保存先
```

## デプロイ手順

### 1. ソースコードをVPSに転送

```bash
# ローカルからVPSへ
scp -r PlaywrightTest1 user@72.60.211.213:~/
```

または Git clone:
```bash
cd ~
git clone https://github.com/yourrepo/PlaywrightTest1.git
```

### 2. Dockerでビルド＆起動

```bash
cd ~/PlaywrightTest1

# ビルド
docker-compose build

# 起動
docker-compose up -d

# ログ確認
docker-compose logs -f
```

### 3. 動作確認

```bash
# ローカルテスト
curl http://localhost:5000/api/test

# 期待されるレスポンス:
# {"success": true, "message": "接続テスト成功 - Playwrightサーバーは正常に動作しています", ...}
```

## Cloudflare Tunnel設定（HTTPS化）

GASからアクセスするにはHTTPSが必要です。

### 1. Cloudflare Tunnelトークン取得

1. Cloudflare Zero Trust ダッシュボードにアクセス
2. Access → Tunnels → Create a tunnel
3. トークンをコピー

### 2. docker-compose.yml を編集

```yaml
services:
  kaipoke-api:
    # ... 既存の設定 ...

  cloudflared:
    image: cloudflare/cloudflared:latest
    container_name: cloudflared
    restart: unless-stopped
    command: tunnel --no-autoupdate run --token YOUR_TUNNEL_TOKEN
    depends_on:
      - kaipoke-api
```

### 3. 再起動

```bash
docker-compose down
docker-compose up -d
```

### 4. GASのURL設定

```javascript
var API_BASE_URL = "https://kaipoke-api.your-domain.com";
```

## APIエンドポイント

| メソッド | パス | 説明 |
|---------|------|------|
| GET/POST | /api/test | 接続テスト |
| GET | /api/status | サーバー状態確認 |
| POST | /api/expand | 月間スケジュール展開 |
| POST | /api/export | CSV出力 |
| POST | /api/diff | 差分確認 |
| POST | /api/apply | 差分適用 |

## トラブルシューティング

### コンテナが起動しない

```bash
# ログを確認
docker-compose logs kaipoke-api

# コンテナに入って確認
docker exec -it kaipoke-api bash
```

### Playwrightエラー

```bash
# ブラウザが正しくインストールされているか確認
docker exec -it kaipoke-api playwright install --with-deps firefox
```

### セッション切れ

`state.json` を最新のものに更新してください。
ローカルでカイポケにログインし、生成された `state.json` をVPSにコピー。

```bash
scp state.json user@72.60.211.213:~/PlaywrightTest1/
docker-compose restart
```

## セキュリティ注意

- `state.json` にはカイポケのセッション情報が含まれます
- 本番環境では適切なアクセス制限を設定してください
- Cloudflare Tunnelを使用する場合、Access Policyで認証を追加することを推奨
