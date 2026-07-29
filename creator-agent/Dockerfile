# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.11.28

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install "uv==${UV_VERSION}"
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --output-file requirements.txt \
    && python -m pip wheel --wheel-dir /wheels -r requirements.txt


FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PATH="/home/mindflow/.local/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 mindflow \
    && useradd \
        --uid 10001 \
        --gid mindflow \
        --create-home \
        --shell /usr/sbin/nologin \
        mindflow

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY --from=builder /build/requirements.txt .
RUN python -m pip install --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY --chown=mindflow:mindflow app ./app
COPY --chown=mindflow:mindflow migrations ./migrations
COPY --chown=mindflow:mindflow alembic.ini ./

RUN mkdir -p /app/data /app/target \
    && chown -R mindflow:mindflow /app/data /app/target

USER mindflow

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=6 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/actuator/health/ready', timeout=2)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
