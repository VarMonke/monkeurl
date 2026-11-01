# The base image. Python 3.13. Slim. Like our tolerance for long URLs.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_DEV=1

WORKDIR /app

# uv does the dependency dance; the lockfile is the source of truth.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
ENV PATH="/app/.venv/bin:$PATH"

# Static assets at build time; settings need no live DB/Redis to import.
RUN DJANGO_SETTINGS_MODULE=monkeurl.settings \
    .venv/bin/python manage.py collectstatic --noinput

RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "monkeurl.wsgi:application", "--bind", "0.0.0.0:8000", \
     "--workers", "4", "--threads", "2", "--timeout", "30", \
     "--access-logfile", "-", "--error-logfile", "-"]
