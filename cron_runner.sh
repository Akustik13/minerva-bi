#!/bin/sh
# cron_runner.sh
#
# Запускається як окремий контейнер у docker-compose.
# Виконує management-команди за розкладом без зовнішнього cron.
#
# Інтервали (секунди):
#   TRACK_INTERVAL  — авто-трекінг відправлень (default: 300 = 5 хв)
#   sync_digikey_orders — інтервал читається з DigiKeyConfig.sync_interval_minutes в БД

set -e

TRACK_INTERVAL="${TRACK_INTERVAL:-300}"

echo "⏳ Waiting for PostgreSQL..."
until pg_isready -h "${DB_HOST:-db}" -p "${DB_PORT:-5432}" \
      -U "${DB_USER:-tabele}" -d "${DB_NAME:-tabele}" -q; do
  sleep 2
done
echo "✅ PostgreSQL ready — cron runner started"
echo "   track_shipments     every ${TRACK_INTERVAL}s"
echo "   sync_digikey_orders interval controlled by DigiKeyConfig in DB"
echo "   send_digest         time/frequency controlled by NotificationSettings in DB"

LAST_TRACK=0

while true; do
  NOW=$(date +%s)

  # ── Авто-трекінг відправлень ─────────────────────────────
  if [ $((NOW - LAST_TRACK)) -ge "$TRACK_INTERVAL" ]; then
    echo "[$(date '+%H:%M:%S')] Running track_shipments..."
    python manage.py track_shipments 2>&1 || true
    LAST_TRACK=$(date +%s)
  fi

  # ── DigiKey синхронізація — інтервал з БД ────────────────
  echo "[$(date '+%H:%M:%S')] Running sync_digikey_orders..."
  python manage.py sync_digikey_orders 2>&1 || true

  # ── Digest report — час і частота керуються в NotificationSettings ──
  python manage.py send_digest 2>&1 || true

  sleep 60
done
