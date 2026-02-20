FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --upgrade pip build && \
    python -m build --wheel --outdir /dist

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN addgroup --system app && \
    adduser --system --ingroup app appuser

WORKDIR /app

COPY --from=builder /dist/*.whl /tmp/wheels/

RUN python -m pip install --no-cache-dir /tmp/wheels/*.whl && \
    rm -rf /tmp/wheels

USER appuser

CMD ["cdc-logical-replication"]
