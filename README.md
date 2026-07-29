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
| `DOMO_EMBED_URL` | the "Domo" tab (optional) | a Domo Card/Page's Share > Embed URL; can also be pasted directly into the tab instead |

Everything works without Mezmo/Freshdesk/Domo credentials except those tabs — the chat and log-upload flow run standalone.

## How the pieces map to the ask

**Upload logs + chat, with slice inspection.** The "Upload Logs" tab drops a file into a shared volume; `logs-mcp` immediately exposes it to the agent. Each file has two actions: "Quick scan" (canned full-file error/anomaly analysis) and "Inspect a slice..." — a small form where you type a specific question and, optionally, a line range (start + count). It asks Aura to read exactly that slice (via `logs_read_log`'s offset/limit) rather than the whole file, and answer only your question, quoting the matching lines.

**Live logs.** The "Live Logs" tab builds a natural-language query ("search Mezmo for ERROR logs from checkout-service over the last hour...") and sends it to the agent, which resolves it against the real Mezmo MCP server (`https://mcp.mezmo.com/mcp`) using `MEZMO_API_KEY`.

**Freshdesk correlation, filtered from the ticket itself.** The "Freshdesk Bugs" tab loads automatically with every ticket from the last 2 days (change the dropdown for 1/7/30 days, or use the search box instead). Click "View" on any row to expand its full description, tags, and timestamps inline — no separate tool needed to read a ticket. Click "Correlate with logs" and, before asking Aura anything, the UI fetches the ticket's full details and prefills an editable filter panel: a service/keyword guess (from the subject), a time window (±30 min around when the ticket was created), and a log-level filter. Adjust anything, then "Run correlation" — the prompt sent to Aura includes the ticket description plus those exact filters, and `config/aura.toml`'s system prompt instructs the agent to treat them as hard constraints rather than re-guessing its own.

**Domo (display only).** The "Domo" tab is a plain iframe embed of a Domo Card/Page — paste a Share > Embed URL (or set `DOMO_EMBED_URL`) and it persists in the browser. No data flows between Aura and Domo; it's just a dashboard panel alongside the rest of the playground.

## Where the prompts live, if you want to tune them further

- Per-action prompt templates (what exact text gets sent to chat for each button) are in `web/public/index.html`'s `<script>` — search for `sendChat(` calls.
- The agent's overall behavior (tool usage, citing sources, how strictly to honor filters) is `system_prompt` in `config/aura.toml`.

## Extending

- Add more MCP servers by adding `[mcp.servers.<name>]` blocks to `config/aura.toml` — see `mezmo/aura`'s `examples/reference.toml` and `examples/complete/` for the full option set (headers, `headers_from_request`, orchestration, vector stores, etc.).
- Swap the single-agent config for orchestration mode (coordinator + workers) by following the pattern in `mezmo/aura`'s `quickstart.toml`.
- The control panel is intentionally plain (no build step) — `web/public/index.html` is a single static file you can edit directly.
