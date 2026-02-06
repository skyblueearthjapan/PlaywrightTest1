#!/bin/bash
# VNC環境起動スクリプト

set -e

# 環境変数のデフォルト値
DISPLAY=${DISPLAY:-:99}
SCREEN_WIDTH=${SCREEN_WIDTH:-1280}
SCREEN_HEIGHT=${SCREEN_HEIGHT:-720}
SCREEN_DEPTH=${SCREEN_DEPTH:-24}
VNC_PORT=${VNC_PORT:-5901}
NOVNC_PORT=${NOVNC_PORT:-6080}

echo "=== VNC Environment Starting ==="
echo "Display: $DISPLAY"
echo "Screen: ${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH}"
echo "VNC Port: $VNC_PORT"
echo "noVNC Port: $NOVNC_PORT"

# 既存プロセスをクリーンアップ
pkill -9 Xvfb || true
pkill -9 x11vnc || true
pkill -9 fluxbox || true
rm -f /tmp/.X99-lock || true

# Xvfb（仮想ディスプレイ）を起動
echo "Starting Xvfb..."
Xvfb $DISPLAY -screen 0 ${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH} &
sleep 2

# Fluxbox（軽量ウィンドウマネージャー）を起動
echo "Starting Fluxbox..."
fluxbox -display $DISPLAY &
sleep 1

# x11vnc（VNCサーバー）を起動
echo "Starting x11vnc..."
x11vnc -display $DISPLAY -rfbport $VNC_PORT -nopw -shared -forever -bg
sleep 1

# websockify + noVNC を起動
echo "Starting noVNC (websockify)..."
websockify --web=/usr/share/novnc $NOVNC_PORT localhost:$VNC_PORT &

echo "=== VNC Environment Ready ==="
echo "noVNC URL: http://localhost:$NOVNC_PORT/vnc.html"

# プロセスを維持
wait
