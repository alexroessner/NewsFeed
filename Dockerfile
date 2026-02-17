# Multi-stage Dockerfile for NewsFeed
# Stage 1: Build dependencies
FROM python:3.11-slim AS builder

WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir -e ".[all]"

# Stage 2: Production runtime
FROM python:3.11-slim AS runtime

WORKDIR /app

# Security: run as non-root
RUN groupadd -r newsfeed && useradd -r -g newsfeed -d /app newsfeed

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY src/ src/
COPY config/ config/
COPY personas/ personas/
COPY pyproject.toml .

# Create state directory for SQLite (when not using D1)
RUN mkdir -p /app/state && chown -R newsfeed:newsfeed /app

USER newsfeed

# Health check endpoint (via Telegram bot polling health)
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# Environment variables (override in docker-compose or K8s)
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
ENV NEWSFEED_LOG_JSON=1

ENTRYPOINT ["python", "-m", "newsfeed.orchestration.bootstrap"]
