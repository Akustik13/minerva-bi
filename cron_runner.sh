#!/bin/sh
# cron_runner.sh
#
# Запускається як окремий контейнер у docker-compose.
# Виконує management-команди за розкладом без зовнішнього cron.
#
# Інтервали (секунди):
#   TRACK_INTERVAL  — авто-трекінг відправлень (default: 300 = 5 хв)
#   SYNC_INTERVAL   — DigiKey синхронізація   (default: 1800 = 30 хв)

set -e

TRACK_INTERVAL="${TRACK_INTERVAL:-300}"
SYNC_INTERVAL="${SYNC_INTERVAL:-1800}"

echo "⏳ Waiting for PostgreSQL..."
until pg_isready -h "${DB_HOST:-db}" -p "${DB_PORT:-5432}" \
      -U "${DB_USER:-tabele}" -d "${DB_NAME:-tabele}" -q; do
  sleep 2
done
echo "✅ PostgreSQL ready — cron runner started"
echo "   track_shipments     every ${TRACK_INTERVAL}s"
echo "   sync_digikey_orders every ${SYNC_INTERVAL}s"

LAST_TRACK=0
LAST_SYNC=0

while true; do
  NOW=$(date +%s)

  # ── Авто-трекінг відправлень ─────────────────────────────
  if [ $((NOW - LAST_TRACK)) -ge "$TRACK_INTERVAL" ]; then
    echo "[$(date '+%H:%M:%S')] Running track_shipments..."
    python manage.py track_shipments 2>&1 || true
    LAST_TRACK=$(date +%s)
  fi

  # ── DigiKey синхронізація ────────────────────────────────
  if [ $((NOW - LAST_SYNC)) -ge "$SYNC_INTERVAL" ]; then
    echo "[$(date '+%H:%M:%S')] Running sync_digikey_orders..."
    python manage.py sync_digikey_orders 2>&1 || true
    LAST_SYNC=$(date +%s)
  fi

  sleep 60
done
