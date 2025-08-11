#!/bin/bash
set -e

# Run migrations only for web server
if [[ "$1" = "gunicorn" ]]; then
  echo "🧱 Running migrations..."
  python manage.py migrate --noinput
fi

# Exec the actual command passed in
exec "$@"