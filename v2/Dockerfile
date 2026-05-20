FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8088

WORKDIR /app

# Install system deps (ca-certificates for SSL, nodejs for playwright-cli)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl nodejs npm \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install playwright-cli
RUN npm install -g @playwright/cli@latest \
    && playwright-cli install --skills 2>/dev/null || true

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade certifi pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

EXPOSE 8088
CMD ["python", "main.py"]
