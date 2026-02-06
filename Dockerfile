# カイポケ自動化 API サーバー
# Playwright + Flask + VNC on Docker

FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

WORKDIR /app

# タイムゾーン設定（対話入力をスキップ）
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Tokyo

# VNC関連パッケージをインストール
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    xvfb \
    x11vnc \
    fluxbox \
    supervisor \
    novnc \
    websockify \
    net-tools \
    curl \
    && rm -rf /var/lib/apt/lists/*

# noVNCのシンボリックリンク作成
RUN ln -s /usr/share/novnc/vnc.html /usr/share/novnc/index.html

# 依存関係をインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションコードをコピー
COPY . .

# ディレクトリを作成
RUN mkdir -p /app/data /app/artifacts /app/logs /var/log/supervisor

# supervisord設定をコピー
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# VNC起動スクリプトをコピー
COPY scripts/start-vnc.sh /usr/local/bin/start-vnc.sh
RUN chmod +x /usr/local/bin/start-vnc.sh

# ポート公開
# 5000: Flask API
# 6080: noVNC (WebSocket)
EXPOSE 5000 6080

# 環境変数
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV DISPLAY=:99
ENV VNC_PORT=5901
ENV NOVNC_PORT=6080
ENV SCREEN_WIDTH=1280
ENV SCREEN_HEIGHT=720
ENV SCREEN_DEPTH=24

# ヘルスチェック
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5000/api/status || exit 1

# supervisordで複数プロセスを管理
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
