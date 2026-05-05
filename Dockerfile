FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8088

WORKDIR /app

# Install Node.js 22 for MCP server and playwright-cli
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install Node.js dependencies (playwright-cli + MCP server)
COPY package.json package-lock.json* ./
RUN npm install --omit=dev

# Install MCP server dependencies
COPY azure-playwright-service-mcp/package.json azure-playwright-service-mcp/
RUN cd azure-playwright-service-mcp && npm install --omit=dev

# Copy application code
COPY . .

EXPOSE 8088
CMD ["python", "main.py"]
