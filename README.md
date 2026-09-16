# aura-mezmo

A playground to test out the features of [Aura by Mezmo](https://github.com/mezmo/aura): upload logs and chat with an agent about them, hook up live Mezmo log search, and correlate Freshdesk bug tickets with log evidence.

## What's here

- **`config/aura.toml`** — the Aura agent config. Registers three MCP tool sources: `mezmo` (hosted, live log search/export), `logs` (local, uploaded files), `freshdesk` (local, bug ticket search).
- **`web/`** — a small FastAPI app serving the control-panel UI (`web/public/index.html`): a chat panel plus tabs for uploading logs, querying live logs, searching Freshdesk, and authoring skills.
- **`services/logs-mcp/`** — MCP server exposing uploaded log files as tools (`list_uploaded_logs`, `read_log`, `search_logs`).
- **`services/freshdesk-mcp/`** — MCP server wrapping the Freshdesk API v2 (`search_tickets`, `get_ticket`, `list_recent_tickets`).
- **`docker-compose.yml`** — wires all of the above together plus the `mezmo/aura:latest` agent image.
- **`config/skills/`** — Agent Skills (agentskills.io spec, same format Claude uses): folders of `SKILL.md` files with static domain knowledge the agent loads on demand via `load_skill`/`read_skill_file`, rather than always sitting in the system prompt. Ships with `robot-shift-notes` — a log-keyword reference + output format for generating plain-English shift notes from robot/Mezmo logs.

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
| `MEZMO_API_KEY` | the "Live Logs" tab | goes to the `mezmo-proxy` container only, never to `aura`; omit and the proxy refuses to start |
| `FRESHDESK_DOMAIN`, `FRESHDESK_API_KEY` | the "Freshdesk Bugs" tab | domain is the subdomain, e.g. `acme` for `acme.freshdesk.com` |

Everything works without Mezmo/Freshdesk credentials except those tabs — the chat, log-upload, and skills flows run standalone.

## How the pieces map to the ask

**Upload logs + chat, with slice inspection.** The "Upload Logs" tab drops a file into a shared volume; `logs-mcp` immediately exposes it to the agent. Each file has two actions: "Quick scan" (canned full-file error/anomaly analysis) and "Inspect a slice..." — a small form where you type a specific question and, optionally, a line range (start + count). It asks Aura to read exactly that slice (via `logs_read_log`'s offset/limit) rather than the whole file, and answer only your question, quoting the matching lines.

**Live logs, production only.** The "Live Logs" tab builds a natural-language query ("search Mezmo for ERROR logs from checkout-service over the last hour...") and sends it to the agent, which resolves it against `mezmo-proxy` — not against `https://mcp.mezmo.com/mcp` directly.

`mezmo-proxy` (`services/mezmo-proxy`) is a read-only gateway in front of Mezmo's hosted MCP server, and it enforces two things in Python that neither the system prompt nor the agent can relax:

- **Production only.** Asking for a dev robot (`gen1-dev*`), a prototype (`gen1-proto*`), or any non-`gen1` host returns an explicit out-of-scope error.
- **Noise filtered.** Every query gets this AND-ed onto it before it leaves the network:

  ```
  host:gen1-prod -level:debug -(level:info (app:fastloop OR app:pickle_rosbridge))
  ```

  Dropping DEBUG removes ~80% of line volume (9,166,715 → 1,778,362 over an 8h fleet-wide window). Suppressing INFO from the two highest-volume apps removes a further ~50% (11,825,178 → 5,945,928 on a pinned 8h window). Note the side effect: ~99.9% of `fastloop`'s non-DEBUG output is INFO, so fastloop is almost entirely absent from the agent's view — the system prompt tells it to say so rather than conclude fastloop was idle.
- **Read only.** Mezmo exposes 32 tools on this account; the proxy re-exposes 9. The ~20 pipeline mutators (`create_pipeline`, `pause_pipeline`, `delete_pipeline_component`, `create_pipeline_access_key`, ...) plus `tap_pipeline_component` are not merely refused — they aren't on the tool list the agent sees, so nothing here can change existing pipelines.

`MEZMO_API_KEY` is deliberately blanked in the `aura` container and given only to the proxy; otherwise the agent could reach Mezmo directly and both guarantees above would be advisory. Tool names, descriptions and input schemas are discovered live from Mezmo at startup and re-exposed verbatim, so Mezmo's query-syntax documentation still reaches the agent intact.

**Freshdesk correlation, filtered from the ticket itself.** The "Freshdesk Bugs" tab loads automatically with every ticket from the last 2 days (change the dropdown for 1/7/30 days, or use the search box instead). Click "View" on any row to expand its full description, tags, and timestamps inline — no separate tool needed to read a ticket. Click "Correlate with logs" and, before asking Aura anything, the UI fetches the ticket's full details and prefills an editable filter panel: a service/keyword guess (from the subject), a time window (±30 min around when the ticket was created), and a log-level filter. Adjust anything, then "Run correlation" — the prompt sent to Aura includes the ticket description plus those exact filters, and `config/aura.toml`'s system prompt instructs the agent to treat them as hard constraints rather than re-guessing its own.

**Skills, authored from the browser.** The "Skills" tab is a UI-only front end for `config/skills/` — meant for sharing this playground with people who get the chatbot but not code access. It lists every skill (including the built-in `robot-shift-notes`, marked "built-in"), and "+ New skill" opens a form for a name, a description (what it does + when Aura should reach for it — this sits in Aura's system prompt at all times, so it's worth being specific), and Markdown instructions (a starter template is prefilled). Click any card to edit or delete it. Because Aura only discovers skills at startup (see "Skills" below), saving shows a banner reminding whoever is hosting this deployment to run `docker compose restart aura` before the change is live in chat — this app can create/edit the files, but it deliberately can't restart Aura itself, since a shared multi-person deployment shouldn't let any one visitor interrupt everyone else's conversation to test a skill.

## Where the prompts live, if you want to tune them further

- Per-action prompt templates (what exact text gets sent to chat for each button) are in `web/public/index.html`'s `<script>` — search for `sendChat(` calls.
- The agent's overall behavior (tool usage, citing sources, how strictly to honor filters) is `system_prompt` in `config/aura.toml`.
- Stable, long-form domain knowledge (glossaries, runbooks, keyword references) belongs in `config/skills/` instead of the system prompt — see below.

## Skills

Each subdirectory of `config/skills/` is one skill: a `SKILL.md` with a `name` + `description` in its YAML frontmatter, plus the actual instructions as Markdown body. Only the name/description sit in the system prompt at all times (cheap); the full body loads only when the agent calls `load_skill` because a request seems relevant, and any `references/`/`scripts/`/`assets/` files inside the skill load one-by-one via `read_skill_file` if needed.

`robot-shift-notes` ships as an example: a reference table of log keywords (rosbag start/stop, e-stops, package drops, conveyor blocks, motion planning failures, etc.) plus an output format spec, so asking Aura for "shift notes for gen1-prod17's last session" produces a plain-English writeup grounded in actual log search results — not a guess. A few keyword rows are marked `TODO` in that file where the exact search term wasn't provided yet; fill those in as they're confirmed. The skill also tells the agent to group routine activity into counts and only narrate anomalies individually (a raw event-per-line dump over a full day isn't a shift note), and to write a partial note rather than nothing if it runs out of tool-call budget partway through.

### Notes on running this against a full day of logs

Searching a whole day's worth of logs across many keyword categories is a lot of tool calls and can return large results. Three things in this config exist specifically for that:

- `AURA_CUSTOM_EVENTS`, `AURA_EMIT_REASONING: "false"`, and `TOOL_RESULT_MODE: "none"` on the `aura` service in `docker-compose.yml` keep the chat UI showing only the final answer, not the agent's turn-by-turn tool-use narration.
- `turn_depth = 30` in `config/aura.toml` (up from the original default of 5-8) gives an exhaustive sweep enough tool-calling rounds to actually finish.
- `[agent.scratchpad]` with `enabled = true`, plus `memory_dir` and `[agent.llm].context_window`, lets the agent explore large search results incrementally instead of dumping them straight into context (which is what causes an upstream "provider returned an error" failure on big enough inputs). If you switch `LLM_MODEL` to something with a different context limit, update `context_window` to match.

To add another skill, either use the "Skills" tab in the UI (no code access needed — see above), or by hand: `mkdir config/skills/my-skill-name`, add a `SKILL.md` with matching `name` in the frontmatter, and restart the `aura` container. Either way, no other config changes are needed, since `config/aura.toml` already points `[[agent.skills.local]]` at `/app/skills` (mounted from `config/skills/` in `docker-compose.yml`), and a restart is required either way since Aura only scans that directory at startup.

## Sharing this with other people

If you're handing this playground to people who should only get the chatbot/UI — not the repo, not edit access to `config/aura.toml` or the Docker setup — give them the URL for the `web` service (port 3000) and nothing else. The "Upload Logs", "Live Logs", "Freshdesk Bugs", and "Skills" tabs are all just friendlier front ends for things Aura can already do or files it already reads; none of them expose the underlying code or let a visitor touch anything outside `config/skills/`. Two things worth knowing before you do this:

- **No login.** Nothing in this repo gates who can reach the `web` service — anyone with the URL can chat, upload logs, and create/edit/delete skills. That's fine for a small trusted group; put it behind whatever auth (a reverse proxy, a VPN, Tailscale, etc.) makes sense if the audience is bigger than that.
- **Skill changes need a restart.** New or edited skills only take effect after the `aura` container restarts (see above) — a deliberate choice so that no visitor can restart Aura themselves and interrupt everyone else's in-flight conversation. As the operator, restart it yourself (`docker compose restart aura`) after skill edits land, or on whatever cadence makes sense for your group.

## Extending

- Add more MCP servers by adding `[mcp.servers.<name>]` blocks to `config/aura.toml` — see `mezmo/aura`'s `examples/reference.toml` and `examples/complete/` for the full option set (headers, `headers_from_request`, orchestration, vector stores, etc.).
- Swap the single-agent config for orchestration mode (coordinator + workers) by following the pattern in `mezmo/aura`'s `quickstart.toml`.
- The control panel is intentionally plain (no build step) — `web/public/index.html` is a single static file you can edit directly.
