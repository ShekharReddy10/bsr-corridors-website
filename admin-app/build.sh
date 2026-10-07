#!/usr/bin/env bash
# Render build: install, collect static files, migrate, ensure the admin account exists.
set -o errexit
pip install --upgrade pip
pip install -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate --noinput
python manage.py createcachetable
python manage.py ensure_admin
