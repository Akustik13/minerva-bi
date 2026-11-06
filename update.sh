#!/bin/sh
# update.sh — оновлення Minerva на NAS без повного rebuild
#
# Використання:
#   ./update.sh           — git pull + restart web + cron
#   ./update.sh --rebuild — те саме + rebuild образу (якщо змінились requirements.txt)
#
# Приклад після git push з Windows:
#   ssh admin@192.168.2.123 "cd /volume1/docker/tabele_mvp && ./update.sh"

set -e

REBUILD=0
if [ "$1" = "--rebuild" ]; then
  REBUILD=1
fi

echo "📥 Отримання змін з Git..."
git pull origin main

echo ""

if [ $REBUILD -eq 1 ]; then
  echo "🔨 Rebuild Docker образу (requirements.txt змінився)..."
  # cron і telegram-bot — окремі образи з того самого Dockerfile: без них там лишаються старі бібліотеки
  # (напр. немає pypdf — синхронізація пошти не читає PDF у листах)
  docker-compose build --no-cache web cron telegram-bot
  docker-compose up -d web cron
  if docker-compose ps --services --filter "status=running" 2>/dev/null | grep -qx telegram-bot; then
    docker-compose up -d telegram-bot
  fi
else
  echo "🔄 Перезапуск web + cron (без rebuild)..."
  docker-compose restart web cron
fi

echo ""
echo "⏳ Зачекати старту (5 сек)..."
sleep 5

echo ""
echo "📋 Логи web:"
docker-compose logs --tail=20 web

echo ""
echo "✅ Оновлення завершено!"
echo "   URL: https://akustik.synology.me:81/admin/"
