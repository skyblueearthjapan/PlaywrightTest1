# カイポケ自動化 API サーバー
# Playwright + Flask on Docker

FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# 依存関係をインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションコードをコピー
COPY . .

# データディレクトリを作成
RUN mkdir -p /app/data /app/artifacts

# ポート公開
EXPOSE 5000

# 環境変数
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# ヘルスチェック
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/api/status || exit 1

# サーバー起動
CMD ["python", "api_server.py"]
