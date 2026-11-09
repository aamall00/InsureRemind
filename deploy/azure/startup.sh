#!/usr/bin/env bash
set -e
python manage.py migrate --noinput
python manage.py collectstatic --noinput
exec gunicorn insureremind.wsgi --bind=0.0.0.0:8000 \
     --workers 2 --threads 4 --worker-class gthread \
     --timeout 180 --graceful-timeout 30 \
     --access-logfile - --error-logfile -
