# aura-mezmo

A playground to test out the features of [Aura by Mezmo](https://github.com/mezmo/aura): upload logs and chat with an agent about them, hook up live Mezmo log search, and correlate Freshdesk bug tickets with log evidence.

## What's here

- **`config/aura.toml`** — the Aura agent config. Registers three MCP tool sources: `mezmo` (hosted, live log search/export), `logs` (local, uploaded files), `freshdesk` (local, bug ticket search).
- **`web/`** — a small FastAPI app serving the control-panel UI (`web/public/index.html`): a chat panel plus tabs for uploading logs, querying live logs, and searching Freshdesk.
- **`services/logs-mcp/`** — MCP server exposing uploaded log files as tools (`list_uploaded_logs`, `read_log`, `search_logs`).
- **`services/freshdesk-mcp/`** — MCP server wrapping the Freshdesk API v2 (`search_tickets`, `get_ticket`, `list_recent_tickets`).
- **`docker-compose.yml`** — wires all of the above together plus the `mezmo/aura:latest` agent image.

## Quickstart

```bash
cp .env.example .env
# edit .env: LLM_API_KEY (required), MEZMO_API_KEY, FRESHDESK_DOMAIN + FRESHDESK_API_KEY (optional)

docker compose up --build
```

Open **http://localhost:3000** for the control panel.

| Env var | Required for | Notes |
|---|---|---|
| `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL` | the agent to run at all | any provider Aura supports (anthropic, openai, bedrock, ...) |
| `MEZMO_API_KEY` | the "Live Logs" tab | Mezmo's hosted MCP server; omit and that tab's queries will just report no access |
| `FRESHDESK_DOMAIN`, `FRESHDESK_API_KEY` | the "Freshdesk Bugs" tab | domain is the subdomain, e.g. `acme` for `acme.freshdesk.com` |

Everything works without Mezmo/Freshdesk credentials except those two tabs — the chat and log-upload flow run standalone.

## How the three pieces map to the ask

**Upload logs + chat.** The "Upload Logs" tab drops a file into a shared volume; `logs-mcp` immediately exposes it to the agent as a searchable tool. Click "Ask Aura" next to any uploaded file to jump into a chat pre-loaded with an analysis prompt.

**Live logs.** The "Live Logs" tab builds a natural-language query ("search Mezmo for ERROR logs from checkout-service over the last hour...") and sends it to the agent, which resolves it against the real Mezmo MCP server (`https://mcp.mezmo.com/mcp`) using `MEZMO_API_KEY`.

**Freshdesk correlation.** The "Freshdesk Bugs" tab searches tickets directly (for browsing) and offers a "Correlate with logs" button per ticket, which asks the agent to cross-reference that ticket's subject/timing against uploaded and/or live logs and summarize a root cause.

## Extending

- Add more MCP servers by adding `[mcp.servers.<name>]` blocks to `config/aura.toml` — see `mezmo/aura`'s `examples/reference.toml` and `examples/complete/` for the full option set (headers, `headers_from_request`, orchestration, vector stores, etc.).
- Swap the single-agent config for orchestration mode (coordinator + workers) by following the pattern in `mezmo/aura`'s `quickstart.toml`.
- The control panel is intentionally plain (no build step) — `web/public/index.html` is a single static file you can edit directly.
