#!/bin/sh
# cron_runner.sh
#
# Запускається як окремий контейнер у docker-compose.
# Виконує management-команди за розкладом без зовнішнього cron.
#
# Інтервали (секунди):
#   TRACK_INTERVAL  — авто-трекінг відправлень (default: 300 = 5 хв)
#   sync_digikey_orders — інтервал читається з DigiKeyConfig.sync_interval_minutes в БД

TRACK_INTERVAL="${TRACK_INTERVAL:-300}"
REMINDER_INTERVAL="${REMINDER_INTERVAL:-900}"
BRIEFING_HOUR="${BRIEFING_HOUR:-08}"

echo "⏳ Waiting for PostgreSQL..."
until pg_isready -h "${DB_HOST:-db}" -p "${DB_PORT:-5432}" \
      -U "${DB_USER:-tabele}" -d "${DB_NAME:-tabele}" -q; do
  sleep 2
done
echo "✅ PostgreSQL ready — cron runner started"
echo "   track_shipments     every ${TRACK_INTERVAL}s"
echo "   sync_digikey_orders interval controlled by DigiKeyConfig in DB"
echo "   send_digest         time/frequency controlled by NotificationSettings in DB"
echo "   morning_briefing    daily at ${BRIEFING_HOUR}:00"
echo "   send_reminders      every ${REMINDER_INTERVAL}s"

LAST_TRACK=0
LAST_REMINDER=0
last_briefing_day=""

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

  # ── Ранковий брифінг — щоденно о BRIEFING_HOUR:00 ───────
  current_hour=$(date +%H)
  current_day=$(date +%Y-%m-%d)
  if [ "$current_hour" = "$BRIEFING_HOUR" ] && [ "$current_day" != "$last_briefing_day" ]; then
    echo "[$(date '+%H:%M:%S')] Running morning_briefing..."
    python manage.py morning_briefing 2>&1 || true
    last_briefing_day=$current_day
  fi

  # ── Нагадування — кожні REMINDER_INTERVAL секунд ────────
  if [ $((NOW - LAST_REMINDER)) -ge "$REMINDER_INTERVAL" ]; then
    python manage.py send_reminders 2>&1 || true
    python manage.py fetch_emails 2>&1 || true
    python manage.py auto_advance_strategies 2>&1 || true
    LAST_REMINDER=$(date +%s)
  fi

  sleep 60
done
