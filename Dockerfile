# Use Python 3.13 slim image as base
FROM python:3.13-slim

# Environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install system dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (better caching)
COPY requirements.txt ./

# Install dependencies system-wide
RUN pip install --no-cache-dir -r requirements.txt

# Chromium + its OS-level deps for Design Studio's server-side PDF export (Playwright renders the
# real editor canvas headlessly - see app/services/design_export_service.py). Installed once here
# rather than at container start, so a cold start never pays this download.
RUN playwright install --with-deps chromium

# Copy app code
COPY . .

EXPOSE 8000

# Run migrations and start the server
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
