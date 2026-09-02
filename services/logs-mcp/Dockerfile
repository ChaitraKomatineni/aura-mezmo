FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache curl

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# drain3 pins cachetools==4.2.1, which conflicts with fastmcp's own
# cachetools>=5.0.0 requirement. drain3 only touches cachetools.LRUCache/
# cachedmethod (API-stable across versions), so install it with --no-deps
# and let it ride on whatever cachetools fastmcp already installed above.
RUN pip install --no-cache-dir --no-deps "drain3>=0.9.11"

COPY server.py .
COPY template_mining.py .
COPY drain3.ini .

EXPOSE 8091

# Protocol-level healthcheck: verify MCP JSON-RPC responds to initialize
HEALTHCHECK --interval=5s --timeout=5s --retries=10 --start-period=15s \
    CMD curl -sf http://localhost:8091/mcp -X POST \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"healthcheck","version":"1.0"}},"id":1}' \
        > /dev/null || exit 1

CMD ["python", "server.py"]
