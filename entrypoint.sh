#!/bin/sh
set -e

echo "⏳ Waiting for PostgreSQL..."
until pg_isready -h "${DB_HOST:-db}" -p "${DB_PORT:-5432}" -U "${DB_USER:-tabele}" -d "${DB_NAME:-tabele}" -q; do
  sleep 1
done
echo "✅ PostgreSQL is ready"

echo "📦 Running migrations..."
python manage.py migrate --noinput

echo "🗂️  Collecting static files..."
python manage.py collectstatic --noinput --clear

echo "👤 Creating superuser if needed..."
python manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
u = '${DJANGO_SUPERUSER_USERNAME:-admin}'
p = '${DJANGO_SUPERUSER_PASSWORD:-Minerva}'
e = '${DJANGO_SUPERUSER_EMAIL:-admin@minerva.local}'
if not User.objects.filter(username=u).exists():
    User.objects.create_superuser(u, e, p)
    print(f'Superuser \"{u}\" created.')
else:
    print(f'Superuser \"{u}\" already exists.')
"

echo "🚀 Starting Gunicorn..."
exec gunicorn tabele.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers 2 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile -
