FROM python:3.12-slim AS builder

ENV UV_PROJECT_ENVIRONMENT=/venv \
    UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir 'uv==0.12.3'

WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# Exactly the versions pinned in uv.lock (runtime dependencies only).
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim AS runtime

RUN groupadd --gid 10001 harness \
    && useradd --uid 10001 --gid harness --no-create-home --shell /usr/sbin/nologin harness

COPY --from=builder /venv /venv
WORKDIR /app
COPY src ./src
COPY policies ./policies

ENV PATH="/venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HARNESS_AUTH_REQUIRED=1

USER 10001:10001
EXPOSE 8787
CMD ["uvicorn", "harness.app:app", "--host", "0.0.0.0", "--port", "8787"]
