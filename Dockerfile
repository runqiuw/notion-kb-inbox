FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
WORKDIR /srv

COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends libcairo2 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps --only-shell chromium && rm -rf /var/lib/apt/lists/*

COPY app ./app

# Cloud Run sets PORT. One worker is right here: the service scales to zero and
# concurrency is one person sending links.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
