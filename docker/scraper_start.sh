#!/bin/bash
set -e

echo "[scraper] Starting Xvfb virtual display..."
Xvfb :99 -screen 0 1440x900x24 -ac +extension GLX +render -noreset &
XVFB_PID=$!

# Чекаємо поки Xvfb повністю запуститься (до 15 секунд)
echo "[scraper] Waiting for Xvfb to be ready..."
for i in $(seq 1 15); do
    if DISPLAY=:99 xdpyinfo >/dev/null 2>&1; then
        echo "[scraper] Xvfb ready (${i}s)"
        break
    fi
    sleep 1
done

echo "[scraper] Starting VNC server (port 5900)..."
x11vnc -display :99 -forever -nopw -shared \
       -listen 0.0.0.0 -rfbport 5900 \
       -o /tmp/x11vnc.log \
       -bg

# Чекаємо поки x11vnc відкриє порт 5900
for i in $(seq 1 10); do
    if nc -z localhost 5900 2>/dev/null; then
        echo "[scraper] VNC server ready (${i}s)"
        break
    fi
    sleep 1
done

echo "[scraper] Starting noVNC web UI (port 6080)..."
websockify --web=/usr/share/novnc/ \
           --log-file=/tmp/novnc.log \
           6080 localhost:5900 &

echo "[scraper] Starting worker API (port 8888)..."
exec python /app/worker.py
