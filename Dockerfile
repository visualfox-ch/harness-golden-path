FROM python:3.12-slim AS builder

ENV VIRTUAL_ENV=/venv
RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

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
    PYTHONUNBUFFERED=1

USER 10001:10001
EXPOSE 8787
CMD ["uvicorn", "harness.app:app", "--host", "0.0.0.0", "--port", "8787"]
