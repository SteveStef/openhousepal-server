# Use Python 3.13 slim image as base
FROM python:3.13-slim

# Build argument to invalidate cache (set to current timestamp)
ARG CACHEBUST=1

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Create directories before copying code
RUN mkdir -p /app/data /app/logs

# Copy application code (invalidates cache when code changes)
COPY . .

# Run database migrations
RUN python -m alembic upgrade head

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
