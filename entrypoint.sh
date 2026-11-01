#!/bin/sh
set -e

# idempotent: safe on every boot, and the db healthcheck gates us anyway
python /app/manage.py migrate --noinput
exec "$@"
