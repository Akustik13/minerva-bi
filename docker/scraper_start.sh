#!/bin/bash
set -e

echo "[scraper] Starting Xvfb virtual display..."
Xvfb :99 -screen 0 1440x900x24 -ac &
sleep 2

echo "[scraper] Starting VNC server (port 5900)..."
x11vnc -display :99 -forever -nopw -shared \
       -listen 0.0.0.0 -rfbport 5900 \
       -quiet &

echo "[scraper] Starting noVNC web UI (port 6080)..."
websockify --web=/usr/share/novnc/ \
           --log-file=/tmp/novnc.log \
           6080 localhost:5900 &

echo "[scraper] Starting worker API (port 8888)..."
exec python /app/worker.py
