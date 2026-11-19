# Fallback deploy (only if the native Python runtime can't run Chromium): Playwright's official image
# already contains Chromium and its system libraries.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
