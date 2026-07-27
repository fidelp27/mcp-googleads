FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

WORKDIR /app

COPY pyproject.toml README.md DESIGN.md LICENSE google_ads_server.py ./
COPY agency ./agency
COPY config ./config
COPY docs ./docs
RUN python -m pip install --no-cache-dir .

EXPOSE 8000

CMD ["google-ads-mcp"]
