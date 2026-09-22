# aura-mezmo

A playground to test out the features of [Aura by Mezmo](https://github.com/mezmo/aura), wired up against the Pickle Robot production fleet: chat with an agent that can search live Mezmo logs, check whether a robot is actually online, read uploaded log files, and root-cause Freshdesk bug tickets against log evidence.

**If you're here to run it and use the chat:** [Quickstart](#quickstart), then open the control panel and type. The one thing worth reading beyond that is ["Live logs, production only"](#how-the-pieces-map-to-the-ask) — it explains why the agent can only see ~3% of the fleet's log volume, and why that's deliberate rather than a bug to fix.

## What's here

- **`config/aura.toml`** — the Aura agent config. Registers four MCP tool sources: `mezmo` (live log search, via the local proxy below — *not* Mezmo's hosted endpoint), `logs` (local, uploaded files), `freshdesk` (local, bug ticket search), and `fleet` (local, is-this-robot-online).
- **`web/`** — a small FastAPI app serving the control-panel UI (`web/public/index.html`): a chat panel plus tabs for uploading logs, querying live logs, searching Freshdesk, and authoring skills.
- **`services/logs-mcp/`** — MCP server exposing uploaded log files as tools (`list_uploaded_logs`, `read_log`, `search_logs`).
- **`services/mezmo-proxy/`** — read-only, production-only gateway in front of `mcp.mezmo.com`. The one component worth understanding before you change anything; see ["Live logs, production only"](#how-the-pieces-map-to-the-ask) below.
- **`services/freshdesk-mcp/`** — MCP server wrapping the Freshdesk API v2 (`search_tickets`, `get_ticket`, `list_recent_tickets`). `get_ticket` also returns `rca_hints`: the robot and the *failure* window resolved server-side, which is not the same as when the ticket was filed.
- **`services/fleet-status-mcp/`** — MCP server answering "is `gen1-prod17` actually on right now?" by reading the Robot Operations Center dashboard's HTTP API over Tailscale. Optional: without it the agent can still read logs, it just can't distinguish "no logs because nothing went wrong" from "no logs because the robot was off".
- **`docker-compose.yml`** — wires all of the above together plus the `mezmo/aura:latest` agent image.
- **`scripts/replay_report.py`** — re-sends a bug report's recorded queries against the live stack, so you can tell a bad query apart from a filtered-out answer. See ["Bug reports"](#bug-reports-when-someone-gets-a-wrong-answer) below.
- **`config/skills/`** — Agent Skills (agentskills.io spec, same format Claude uses): folders of `SKILL.md` files with static domain knowledge the agent loads on demand via `load_skill`/`read_skill_file`, rather than always sitting in the system prompt. Ships with two: `log-app-reference` (which app/source/level to query for a given question) and `robot-shift-notes` (log-keyword reference + output format for plain-English shift notes).

## Quickstart

```bash
cp .env.example .env
# edit .env: LLM_API_KEY and MEZMO_API_KEY (both required to start),
#            FRESHDESK_* and ROC_DASHBOARD_URL optional

docker compose up --build
```

Open **http://localhost:8000** for the control panel. (Aura's own API is on **8081**, not 8080 — see the comment in `docker-compose.yml`; you don't normally need it.)

| Env var | Required for | Notes |
|---|---|---|
| `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL` | the agent to run at all | any provider Aura supports; `.env.example` is set up for `openrouter` |
| `MEZMO_API_KEY` | **the stack to start at all** | goes to the `mezmo-proxy` container only, never to `aura`. Compose refuses to start without it — it's `${MEZMO_API_KEY:?...}`, not optional |
| `FRESHDESK_DOMAIN`, `FRESHDESK_API_KEY` | the "Freshdesk Bugs" tab | the bare subdomain (`acme` for `acme.freshdesk.com`) or a full custom portal domain |
| `ROC_DASHBOARD_URL` | the `fleet` tools | defaults to `http://roc-ubu:3001`, which only resolves if this host is on the Tailnet with MagicDNS. The container starts either way; the tools just return an error |

Freshdesk and fleet-status credentials are optional — the chat, log-upload, live-log, and skills flows all work without them. `MEZMO_API_KEY` is not optional, because the proxy has nothing to proxy without it.

## How the pieces map to the ask

**Upload logs + chat, with slice inspection.** The "Upload Logs" tab drops a file into a shared volume; `logs-mcp` immediately exposes it to the agent. Each file has two actions: "Quick scan" (canned full-file error/anomaly analysis) and "Inspect a slice..." — a small form where you type a specific question and, optionally, a line range (start + count). It asks Aura to read exactly that slice (via `logs_read_log`'s offset/limit) rather than the whole file, and answer only your question, quoting the matching lines.

**Live logs, production only.** The "Live Logs" tab builds a natural-language query ("search Mezmo for ERROR logs from checkout-service over the last hour...") and sends it to the agent, which resolves it against `mezmo-proxy` — not against `https://mcp.mezmo.com/mcp` directly.

`mezmo-proxy` (`services/mezmo-proxy`) is a read-only gateway in front of Mezmo's hosted MCP server. It enforces three things in Python that neither the system prompt nor the agent can relax:

- **Production only.** Asking for a dev robot (`gen1-dev*`), a prototype (`gen1-proto*`), or any non-`gen1-prod` host returns an explicit out-of-scope error. A bare `host:gen1-prod2` is also rewritten to `host:==gen1-prod2`, because Mezmo matches string fields by *prefix* — without that rewrite, asking about `prod2` silently returns `prod20`–`prod29` as well.
- **Noise filtered.** Every query is wrapped in parentheses and then AND-ed with a scope clause before it leaves the network. **The clause is not reproduced here** — it changes, and a copy in a README is a copy that goes stale. The proxy prints it at startup, and exposes it at runtime as the `describe_scope` tool. To read it:

  ```bash
  docker compose logs mezmo-proxy | head -20
  ```

  As of this writing it drops DEBUG fleet-wide, drops `pickle_rosbridge` entirely, drops INFO from `fastloop`/`path_planning`/`action_planning`, gates 13 housekeeping apps (kernel, ssh, audit, tailscaled, rosbag, ...) to error-or-worse, and suppresses one known-benign temperature-probe error. Net effect on a measured fleet day (Fri 2026-09-18): **120,767,854 raw lines → 3,256,497**, or 2.7%.

  Two consequences worth knowing, both of which the system prompt tells the agent to state rather than paper over: `fastloop` is nearly absent from the agent's view (almost all its non-DEBUG output is INFO), and "no logs" never by itself means "nothing happened".

  **If you are tempted to loosen the filters:** the reason they're this aggressive isn't tidiness. Mezmo rejects any query matching more than **1,000,000 lines** outright, and an unfiltered fleet hour peaked at 1,007,004. Under the raw feed the analysis tools don't return worse answers, they return `Query Too Large` and nothing at all. Each exclusion in `server.py` carries a comment with the measurement that justified it; please add one if you add an exclusion, and re-measure with `get_log_histogram` if you remove one.
- **Read only.** Mezmo exposes 32 tools on this account; the proxy serves 11 (10 proxied + `describe_scope`). The ~20 pipeline mutators (`create_pipeline`, `pause_pipeline`, `delete_pipeline_component`, `create_pipeline_access_key`, ...) plus `tap_pipeline_component` are not merely refused — they aren't on the tool list the agent sees, so nothing here can change existing pipelines.

`MEZMO_API_KEY` is deliberately blanked in the `aura`, `web`, and `freshdesk-mcp` containers and given only to the proxy; otherwise the agent could reach `mcp.mezmo.com` directly and all three guarantees above would be advisory rather than actual. **That blanking is the whole enforcement boundary** — if you add a service that needs the key, or hand the key to `aura` "just to test something", the filters stop being filters.

Tool names, descriptions and input schemas are discovered live from Mezmo on the first request (not at boot) and re-exposed verbatim, so Mezmo's own query-syntax documentation still reaches the agent intact. A tool in `SCOPED_TOOLS` that turns out to have no `query` parameter is refused at startup rather than registered unscoped.

**Freshdesk correlation, top-down by severity.** The "Freshdesk Bugs" tab loads automatically with every ticket from the last 2 days (change the dropdown for 1/7/30 days, or use the search box instead). Click "View" on any row to expand its full description, tags, and timestamps inline — no separate tool needed to read a ticket. Click "Correlate with logs" and it goes **straight to chat**; there is no filter form to fill in first.

The window and the robot are resolved server-side instead, by `freshdesk-mcp`'s `rca_hints`, in this priority order: explicit override fields on the ticket → a `Report Time:` line parsed out of the description (ET → UTC) → `created_at` as a last resort, flagged with a warning. That ordering exists because `created_at` is when someone *filed* the ticket, which can be hours after the fault — ticket #7905 was filed 5 hours late, and searching the filing window found nothing while the real window held a FATAL at the reported minute.

The prompt then asks the agent to work **top-down by severity** within that window — FATAL, then CRITICAL, then ERROR, then WARN — and explicitly *not* to keyword-search the operator's own wording, which is a symptom in their words rather than log text. The ticket description is used at the end, to judge whether the candidate events actually explain the report, not at the start to pick search terms.

**Skills, authored from the browser.** The "Skills" tab is a UI-only front end for `config/skills/` — meant for sharing this playground with people who get the chatbot but not code access. It lists every skill (the two that ship with the repo are marked "built-in"), and "+ New skill" opens a form for a name, a description (what it does + when Aura should reach for it — this sits in Aura's system prompt at all times, so it's worth being specific), and Markdown instructions (a starter template is prefilled). Click any card to edit or delete it. Because Aura only discovers skills at startup (see "Skills" below), saving shows a banner reminding whoever is hosting this deployment to run `docker compose restart aura` before the change is live in chat — this app can create/edit the files, but it deliberately can't restart Aura itself, since a shared multi-person deployment shouldn't let any one visitor interrupt everyone else's conversation to test a skill.

## Bug reports: when someone gets a wrong answer

Every assistant message in chat has a small **"Report a problem"** button.
It opens a short form — category, an optional note about what they expected,
an optional name — and saves the whole turn. Reports appear in the
**Reports** tab for anyone to read and mark resolved.

**What gets saved is the point.** A report does not just store the question
someone typed; it stores **every tool call Aura made, with its exact
arguments** — the query string, `from_time`, `to_time`, whether it failed and
why — plus the answer, the reasoning, the model, the session id and the token
usage. That is deliberate: almost every wrong answer this stack has produced
was a wrong *time window* or an over-broad field match, and neither is
visible in the final answer. Without the arguments a report tells you that
something was wrong; with them it usually tells you what.

Storage is `reports/reports.jsonl` on the host — a bind mount, not a Docker
volume, so you can `grep` it directly on the machine that runs this. It is
**append-only**: marking a report resolved writes a status line rather than
rewriting history, so both the original report and the fact that someone
triaged it survive. A half-written line (killed mid-append) is skipped rather
than breaking the tab.

To investigate one:

```bash
REPORT_ID=abc123def456 docker compose exec mezmo-proxy python - < scripts/replay_report.py
```

That re-sends each recorded query through `mezmo-proxy` and prints what comes
back now beside what came back then, which separates the three things a
"wrong answer" can be: the query was wrong, the filters hid the data, or the
tools were fine and Aura misread them. Omit `REPORT_ID` to replay the newest.

`reports/` is **gitignored** — a report stores real tool results, which means
real production log lines. Same rule as `experiments/*/out/`.

Two limits worth knowing: there is no login, so the reporter name is
self-declared and unverified — it tells you who to go ask, not who to hold
responsible. And tool results are clipped to 4,000 characters each, with the
original length recorded, so a report never silently fails to save because
one log payload was enormous.

## Why it's built this way

Four decisions look like over-engineering until you know what went wrong without them. If you're about to simplify one of these, read the corresponding note first.

**Why a proxy instead of just telling the agent the rules in the prompt.** A prompt is a request; the agent can misread it, and a future model can ignore it. The scope is injected in Python, after the agent has composed its query and before the request leaves the network, so there is no phrasing that gets around it. The credential lives only in the proxy for the same reason — a rule the agent *could* bypass but is asked not to is documentation, not enforcement.

**Why the filters are this aggressive.** Not neatness. Mezmo rejects any query matching over 1,000,000 lines, and this fleet's peak hour was 1,007,004 unfiltered — so the choice isn't "clean answers vs. noisy answers", it's "answers vs. `Query Too Large`". Every exclusion was measured before being added, and the measurement is in the comment next to it.

**Why the agent is told to say "I can't see that" so often.** Because the filters create blind spots that look exactly like good news. `fastloop` being absent looks like fastloop being quiet; a robot being powered off looks like a robot with no errors. `fleet-status-mcp` exists specifically to break that second ambiguity, using a network path (Tailscale/SSH presence) independent of log shipping — so "no logs" and "not online" can't be confused for each other.

**Why timestamps get this much ceremony.** Three separate bugs in this stack all presented as "the logs are empty", and all three were the window being wrong rather than the logs being absent: a UI control labelled UTC while reading local time, `created_at` used as the failure time, and placeholder text left in override fields. Hence `rca_hints` resolving the window server-side, the `weekday_to_date` field added to `get_current_time`, and the system prompt requiring an actual time-tool call rather than the model's own idea of today's date.

The general lesson, if you're debugging this stack: **the error message usually names the wrong thing.** `missing field from_time` meant a bad enum three layers up. `0 logs` meant a browser timezone label. `prod22 results` meant prefix matching. Look at the bytes on the wire before believing the message.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `MEZMO_API_KEY must be set in .env` on `docker compose up` | exactly what it says — the proxy won't start without it |
| Agent says a robot produced no logs, but you know it was running | wrong window, not missing logs. Ask it to state the absolute UTC window it searched, then check that against reality |
| Agent can't find something you can see in the Mezmo web UI | it's probably filtered. Ask it to call `mezmo_describe_scope` and check |
| `Query Too Large` | the window is too wide for the line volume. Narrow the time range or pin a single `host:==gen1-prodN` |
| A robot search returns other robots' logs | a bare `app:`/`source:` prefix match. Use `==` for an exact match (the proxy does this for `host:` only) |
| `fleet_*` tools error out | `ROC_DASHBOARD_URL` unreachable — this host needs to be on the Tailnet |
| A skill edit doesn't change the agent's behavior | Aura only scans `config/skills/` at startup: `docker compose restart aura` |
| Sporadic `SSE stream ended` from the heavy analysis tools | upstream transport flakiness; retry. It surfaces as a raised exception, not an error result |

## Where the prompts live, if you want to tune them further

- Per-action prompt templates (what exact text gets sent to chat for each button) are in `web/public/index.html`'s `<script>` — search for `sendChat(` calls.
- The agent's overall behavior (tool usage, citing sources, how strictly to honor filters) is `system_prompt` in `config/aura.toml`.
- Stable, long-form domain knowledge (glossaries, runbooks, keyword references) belongs in `config/skills/` instead of the system prompt — see below.

## Skills

Each subdirectory of `config/skills/` is one skill: a `SKILL.md` with a `name` + `description` in its YAML frontmatter, plus the actual instructions as Markdown body. Only the name/description sit in the system prompt at all times (cheap); the full body loads only when the agent calls `load_skill` because a request seems relevant, and any `references/`/`scripts/`/`assets/` files inside the skill load one-by-one via `read_skill_file` if needed.

Two ship with the repo:

- **`log-app-reference`** — which app, source, level and host to query for a given question: what each app does, what it can and can't tell you, verified search keywords, and which apps to cross-reference. Meant to be read *before* guessing an app name. Claims in it are tagged `[VERIFIED n/day]`, `[CONFIRMED]`, or `[UNVERIFIED]` so the agent knows what's measured and what's hearsay.
- **`robot-shift-notes`** — a reference table of log keywords (rosbag start/stop, e-stops, package drops, conveyor blocks, motion planning failures, etc.) plus an output format spec, so asking Aura for "shift notes for gen1-prod17's last session" produces a plain-English writeup grounded in actual log search results, not a guess. It also tells the agent to group routine activity into counts and narrate only anomalies individually (a raw event-per-line dump over a full day isn't a shift note), and to write a partial note rather than nothing if it runs out of tool-call budget partway through. Seven keyword rows are still marked `TODO` where the exact search term hasn't been confirmed; fill those in as they're verified rather than guessing.

Neither skill restates the proxy's filter clause — they point at `mezmo_describe_scope` instead, for the same reason this README doesn't: a second copy is a copy that drifts.

### Notes on running this against a full day of logs

Searching a whole day's worth of logs across many keyword categories is a lot of tool calls and can return large results. Two things in this config exist specifically for that:

- `turn_depth = 30` in `config/aura.toml` (up from the original default of 5-8) gives an exhaustive sweep enough tool-calling rounds to actually finish.
- `[agent.scratchpad]` with `enabled = true`, plus `memory_dir` and `[agent.llm].context_window = 200000`, lets the agent explore large search results incrementally instead of dumping them straight into context (which is what causes an upstream "provider returned an error" failure on big enough inputs). If you switch `LLM_MODEL` to something with a different context limit, update `context_window` to match.

Separately, `AURA_CUSTOM_EVENTS: "true"`, `AURA_EMIT_REASONING: "true"`, and `TOOL_RESULT_MODE: "aura"` on the `aura` service stream the agent's reasoning and tool steps as `aura.*` events. The UI renders them in a collapsed "Show reasoning" disclosure per message, kept separate from the final answer — so on a long sweep you can see which queries it actually ran and over what windows, which is how you catch a wrong-window answer. (An earlier revision suppressed these at the source; the comment in `docker-compose.yml` explains the switch.)

To add another skill, either use the "Skills" tab in the UI (no code access needed — see above), or by hand: `mkdir config/skills/my-skill-name`, add a `SKILL.md` with matching `name` in the frontmatter, and restart the `aura` container. Either way, no other config changes are needed, since `config/aura.toml` already points `[[agent.skills.local]]` at `/app/skills` (mounted from `config/skills/` in `docker-compose.yml`), and a restart is required either way since Aura only scans that directory at startup.

## Sharing this with other people

If you're handing this playground to people who should only get the chatbot/UI — not the repo, not edit access to `config/aura.toml` or the Docker setup — give them the URL for the `web` service (host port 8000) and nothing else. The "Upload Logs", "Live Logs", "Freshdesk Bugs", "Reports", and "Skills" tabs are all just friendlier front ends for things Aura can already do or files it already reads; none of them expose the underlying code or let a visitor touch anything outside `config/skills/` and `reports/`. Four things worth knowing before you do this:

- **No login.** Nothing in this repo gates who can reach the `web` service — anyone with the URL can chat, upload logs, and create/edit/delete skills. That's fine for a small trusted group; put it behind whatever auth (a reverse proxy, a VPN, Tailscale, etc.) makes sense if the audience is bigger than that.
- **Anyone who can reach the chat can read production logs.** The proxy constrains *which* logs (production robots, filtered) but not *who* asks. Treat the URL as carrying the same sensitivity as Mezmo access itself, and don't publish the `mezmo-proxy` port (8093) — it takes no authentication of its own, since it was only ever meant to be reachable from `aura` on the compose network.
- **Feedback comes back through the Reports tab.** Tell people to hit "Report a problem" on any answer that looks wrong rather than describing it to you in chat — the report captures the queries behind the answer, which a verbal description cannot. See ["Bug reports"](#bug-reports-when-someone-gets-a-wrong-answer).
- **Skill changes need a restart.** New or edited skills only take effect after the `aura` container restarts (see above) — a deliberate choice so that no visitor can restart Aura themselves and interrupt everyone else's in-flight conversation. As the operator, restart it yourself (`docker compose restart aura`) after skill edits land, or on whatever cadence makes sense for your group.

## Extending

- Add more MCP servers by adding `[mcp.servers.<name>]` blocks to `config/aura.toml` — see `mezmo/aura`'s `examples/reference.toml` and `examples/complete/` for the full option set (headers, `headers_from_request`, orchestration, vector stores, etc.).
- Swap the single-agent config for orchestration mode (coordinator + workers) by following the pattern in `mezmo/aura`'s `quickstart.toml`.
- The control panel is intentionally plain (no build step) — `web/public/index.html` is a single static file you can edit directly.
- **Changing what the agent can see** means editing the filter constants at the top of `services/mezmo-proxy/server.py`, then `docker compose up -d --build mezmo-proxy`. Measure before and after with `get_log_histogram` on a pinned window and leave the number in a comment, the way the existing exclusions do — a filter without a measurement next to it is one nobody can safely remove later.
- `experiments/` holds the probes these numbers came from: `mcp-tool-behavior/` (per-tool limits and an error taxonomy), `export-api/` (why the bulk `/v2/export` route isn't used — the key authenticates but returns 403 for lack of read permission on the data), `mezmo-mcp-validate/` (query-syntax behavior against the live account), and `drain3-explorer/` (template mining over uploaded logs). Their outputs are gitignored because they carry real host names and line counts; re-run rather than commit.
