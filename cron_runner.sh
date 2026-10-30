#!/bin/sh
# cron_runner.sh
#
# Запускається як окремий контейнер у docker-compose.
# Виконує management-команди за розкладом без зовнішнього cron.
#
# Інтервали (секунди):
#   TRACK_INTERVAL  — авто-трекінг відправлень (default: 300 = 5 хв)
#   sync_jlc_orders         — інтервал читається з JLCConfig.sync_interval_hours в БД
#   sync_digikey_orders     — інтервал читається з DigiKeyConfig.sync_interval_minutes в БД
#   check_digikey_messages  — інтервал читається з DigiKeyConfig.msg_check_interval в БД

TRACK_INTERVAL="${TRACK_INTERVAL:-300}"
REMINDER_INTERVAL="${REMINDER_INTERVAL:-900}"
EMAIL_SYNC_INTERVAL="${EMAIL_SYNC_INTERVAL:-300}"
SCHEDULED_INTERVAL="${SCHEDULED_INTERVAL:-120}"
CALENDAR_REMINDER_INTERVAL="${CALENDAR_REMINDER_INTERVAL:-120}"
# BRIEFING_HOUR — лише запасний: година брифінгу береться з «Налаштувань брифінгу» → «Час відправки»
export BRIEFING_HOUR="${BRIEFING_HOUR:-08}"

echo "⏳ Waiting for PostgreSQL..."
until pg_isready -h "${DB_HOST:-db}" -p "${DB_PORT:-5432}" \
      -U "${DB_USER:-tabele}" -d "${DB_NAME:-tabele}" -q; do
  sleep 2
done
echo "✅ PostgreSQL ready — cron runner started"
echo "   track_shipments         every ${TRACK_INTERVAL}s"
echo "   sync_jlc_orders         interval controlled by JLCConfig.sync_interval_hours in DB"
echo "   sync_digikey_orders     interval controlled by DigiKeyConfig in DB"
echo "   check_digikey_messages  interval controlled by DigiKeyConfig in DB"
echo "   poll_dk_status          interval controlled by DigiKeyConfig.poll_interval_minutes in DB"
echo "   send_webhooks           every loop (~60s) — retries of API webhooks"
echo "   check_stripe_payments   every loop — polls Stripe only while no webhook arrives (interval per shop)"
echo "   index_shop_media        every 5 min — product photos / datasheets for the shop site (when enabled)"
echo "   send_digest             daily / weekly / monthly — day & time from NotificationSettings in DB, once per day"
echo "   morning_briefing        daily at BriefingSettings.send_time (env BRIEFING_HOUR only as fallback)"
echo "   auto_import             every loop — profiles whose next run is due (interval per profile)"
echo "   send_alerts             every ${REMINDER_INTERVAL}s — critical stock / overdue, min interval from NotificationSettings"
echo "   shop_accounts_cleanup   daily — expired shop customer sessions / links"
echo "   expire_offers           daily — sent Angebote past their validity → expired"
echo "   worktime_autofill       daily after hour from DanteSettings — empty work days in askDANTE (when enabled)"
echo "   worktime_sync           daily after the same hour — askDANTE time records and month balances"
echo "   worktime_reports        askDANTE work-time reports by e-mail (schedule in Minerva)"
echo "   worktime_mail_time      daily — time spent on mail categories/projects into askDANTE (auto categories)"
echo "   calendar_sync           iCal subscriptions + Google / Outlook / Minerva sync rules"
echo "   send_reminders          every ${REMINDER_INTERVAL}s"
echo "   sync_email              every ${EMAIL_SYNC_INTERVAL}s"
echo "   send_calendar_reminders every ${CALENDAR_REMINDER_INTERVAL}s"

# Код змонтовано (./:/app) і підхоплюється сам, а цей цикл shell читає лише при старті контейнера.
# Тому після оновлення файлу (git pull) runner перезапускає сам себе — нові команди почнуть працювати без
# `docker-compose restart cron`.
SCRIPT_STAMP=$(stat -c %Y "$0" 2>/dev/null || echo 0)

LAST_TRACK=0
LAST_REMINDER=0
LAST_EMAIL_SYNC=0
LAST_SCHEDULED=0
LAST_CALENDAR=0
last_cleanup_day=""

while true; do
  NOW=$(date +%s)

  # ── Авто-трекінг відправлень ─────────────────────────────
  if [ $((NOW - LAST_TRACK)) -ge "$TRACK_INTERVAL" ]; then
    echo "[$(date '+%H:%M:%S')] Running track_shipments..."
    python manage.py track_shipments 2>&1 || true
    LAST_TRACK=$(date +%s)
  fi

  # ── JLCPCB синхронізація — інтервал з JLCConfig.sync_interval_hours в БД ──
  python manage.py sync_jlc_orders 2>&1 || true

  # ── DigiKey синхронізація — інтервал з БД ────────────────
  python manage.py sync_digikey_orders 2>&1 || true

  # ── DigiKey Messages — інтервал з БД (msg_check_interval) ──
  python manage.py check_digikey_messages 2>&1 || true

  # ── DigiKey poll staged → published — інтервал з БД (poll_interval_minutes) ──
  python manage.py poll_dk_status 2>&1 || true

  # ── DigiKey ціни ← Minerva: перевірка надісланих, автооновлення (інтервал і режим — у «💲 Налаштуваннях цін DigiKey») ──
  python manage.py dk_price_sync --if-due 2>&1 || true

  # ── Вебхуки API — повтори невдалих доставок (кожен цикл, ~60с) ──
  python manage.py send_webhooks 2>&1 || true

  # ── Онлайн-оплати Stripe: поки вебхук не працює — перевірка незавершених (інтервал у картці магазину) ──
  python manage.py check_stripe_payments 2>&1 || true

  # ── Фото й даташити для сайту: нові / змінені / давно не перевірені (сам стежить за інтервалом) ──
  python manage.py index_shop_media --if-due 2>&1 || true

  # ── Звіти: щоденний, тижневий, місячний — день і час у NotificationSettings, кожен раз на день ──
  python manage.py send_digest --period all --verbosity 0 2>&1 || true

  # ── Ранковий брифінг — у «Час відправки» з BriefingSettings, раз на день (стан — last_sent_at у БД) ──
  python manage.py morning_briefing --if-due 2>&1 || true

  # ── Авто-імпорт: профілі, яким настав «Наступний запуск» (інтервал — у профілі) ──
  python manage.py auto_import --all --verbosity 0 2>&1 || true

  # ── Раз на добу: кабінет покупців магазину, журнал API, прострочені Angebote ──
  current_day=$(date +%Y-%m-%d)
  if [ "$current_day" != "$last_cleanup_day" ]; then
    python manage.py shop_accounts_cleanup 2>&1 || true
    python manage.py api_log_cleanup 2>&1 || true        # 📜 Журнал API: старші за API_LOG_DAYS
    python manage.py expire_offers 2>&1 || true          # 📝 Angebote: строк дії минув → «Прострочено»
    last_cleanup_day=$current_day
  fi

  # ── Робочий час askDANTE: порожні робочі дні — раз на добу (режим і година — у налаштуваннях askDANTE) ──
  python manage.py worktime_autofill --if-due 2>&1 || true
  python manage.py worktime_sync --if-due 2>&1 || true
  python manage.py worktime_reports --if-due 2>&1 || true
  python manage.py worktime_mail_time --if-due 2>&1 || true
  python manage.py calendar_sync --if-due 2>&1 || true

  # ── Email синхронізація — кожні EMAIL_SYNC_INTERVAL секунд ──
  if [ $((NOW - LAST_EMAIL_SYNC)) -ge "$EMAIL_SYNC_INTERVAL" ]; then
    python manage.py sync_email --folders 2>&1 || true
    python manage.py knowledge_refresh --if-due 2>&1 || true   # база знань RAG: раз на добу, якщо використовується
    python manage.py knowledge_sources --if-due 2>&1 || true   # «📂 Джерела знань»: папки й сайти за їх розкладом
    LAST_EMAIL_SYNC=$(date +%s)
  fi

  # ── Заплановані листи — кожні SCHEDULED_INTERVAL секунд ──
  if [ $((NOW - LAST_SCHEDULED)) -ge "$SCHEDULED_INTERVAL" ]; then
    python manage.py send_scheduled 2>&1 || true
    LAST_SCHEDULED=$(date +%s)
  fi

  # ── Нагадування — кожні REMINDER_INTERVAL секунд ────────
  if [ $((NOW - LAST_REMINDER)) -ge "$REMINDER_INTERVAL" ]; then
    python manage.py send_reminders 2>&1 || true
    python manage.py send_alerts --if-due 2>&1 || true   # критичний склад / прострочені: інтервал у NotificationSettings
    python manage.py fetch_emails 2>&1 || true
    python manage.py auto_advance_strategies 2>&1 || true
    LAST_REMINDER=$(date +%s)
  fi

  # ── Календарні нагадування — кожні CALENDAR_REMINDER_INTERVAL секунд ──
  if [ $((NOW - LAST_CALENDAR)) -ge "$CALENDAR_REMINDER_INTERVAL" ]; then
    python manage.py send_calendar_reminders 2>&1 || true
    LAST_CALENDAR=$(date +%s)
  fi

  sleep 60

  if [ "$(stat -c %Y "$0" 2>/dev/null || echo 0)" != "$SCRIPT_STAMP" ]; then
    echo "[$(date '+%H:%M:%S')] cron_runner.sh оновлено — перезапуск з новим розкладом"
    exec sh "$0"
  fi
done
