# One web image. The same image runs migrate and collectstatic at deploy time.
FROM python:3.12-slim AS runtime

RUN pip install --no-cache-dir uv
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY manage.py ./
COPY opshub ./opshub
COPY core ./core
COPY accounts ./accounts
COPY audit ./audit
COPY tasks ./tasks
COPY calendar_app ./calendar_app
COPY mail ./mail
COPY templates ./templates

ENV PATH="/app/.venv/bin:$PATH" DJANGO_SETTINGS_MODULE=opshub.settings
EXPOSE 8000
CMD ["gunicorn", "opshub.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60"]
