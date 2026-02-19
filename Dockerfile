# Use Python 3.13 slim image as base
FROM python:3.13-slim

# Install uv for extremely fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    UV_SYSTEM_PYTHON=1

# Set work directory
WORKDIR /app

# Install system dependencies (needed for some Python packages)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy pyproject.toml and uv.lock first for optimal caching
COPY pyproject.toml uv.lock ./

# Install dependencies without installing the project itself
# This layer is cached unless pyproject.toml or uv.lock changes
RUN uv sync --frozen --no-install-project

# Create directories before copying code
RUN mkdir -p /app/data /app/logs

# Copy application code
COPY . .

# Final sync to install the project
RUN uv sync --frozen

EXPOSE 8000

# Run migrations at startup, then start the application
# We don't need 'uv run' since we're using the system python
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
