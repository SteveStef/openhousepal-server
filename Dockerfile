# Use Python 3.13 slim image as base
FROM python:3.13-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Set work directory
WORKDIR /app

# Install system dependencies (needed for PostgreSQL and building packages)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements.txt first for optimal caching
COPY requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Create directories for logs and data
RUN mkdir -p /app/data /app/logs

# Copy application code
COPY . .

EXPOSE 8000

# Run migrations at startup, then start the application
# We use uvicorn via python -m for extra reliability
CMD ["sh", "-c", "alembic upgrade head && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"]
