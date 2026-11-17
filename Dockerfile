FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

# Install system dependencies (curl for healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements / pyproject first for layer caching
COPY pyproject.toml README.md /app/

# Install python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir ".[dev]"

# Copy application source and migrations
COPY config /app/config
COPY migrations /app/migrations
COPY alembic.ini /app/alembic.ini
COPY src /app/src
COPY scripts /app/scripts

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run migrations and start app
CMD ["sh", "-c", "alembic upgrade head && uvicorn llm_gateway.main:app --host 0.0.0.0 --port 8000"]

