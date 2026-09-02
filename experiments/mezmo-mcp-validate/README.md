# Mezmo MCP validation

Standalone script (not part of the docker-compose stack) that connects to
Mezmo's hosted MCP server (`https://mcp.mezmo.com/mcp`) as a plain client —
the same role Aura plays — and captures the **exact raw tool output** for
the clustering/dedup-related tools, before deciding whether to trust them
in `config/aura.toml`.

## Why

Mezmo's dedup/clustering algorithm isn't documented — no similarity
threshold, no description of the matching logic, unlike our own Drain3
pipeline (`services/logs-mcp`), which was hand-tuned (`sim_th=0.9`) against
a real side-by-side test on actual log files before we trusted it. This
script runs the same kind of check against Mezmo: call the real tools
against your real account, and produce output you can inspect yourself
rather than a description of what the tools are supposed to do.

## What it produces

Two files per run, timestamped:

- `mezmo_raw_output_<timestamp>.json` — the literal MCP `CallToolResult`
  for every tool call, exactly as the server returned it over the wire.
  This is byte-for-byte what a client — including Aura — receives; nothing
  here is summarized or reshaped.
- `mezmo_report_<timestamp>.html` — the same data, readable: which tools
  exist on your account, the arguments sent to each, a parsed table where
  the response was list-of-records JSON, and the full raw JSON tucked into
  a collapsible section per call so nothing is hidden even in the readable
  version.

It also writes the full live `tools/list` schema into the raw JSON file —
useful on its own, since it resolves naming/parameter uncertainty that
Mezmo's own docs don't spell out in raw JSON terms.

## Running it

This needs network access to `mcp.mezmo.com`, which the sandbox this was
written in does not have (outbound calls there return `403`). Run it
somewhere that can reach it — your machine, or from inside the stack:

```bash
pip install -r requirements.txt
export MEZMO_API_KEY=sts_...        # same key as the repo's .env
python3 validate_mezmo.py --minutes 30 --query "" --field app --out-dir ./out
```

Or via the existing Docker stack, from the repo root (after `docker compose
up`), so you don't need Python set up locally at all:

```bash
docker compose cp validate_mezmo.py aura:/tmp/validate_mezmo.py
docker compose exec aura sh -c "pip install fastmcp && python3 /tmp/validate_mezmo.py --out-dir /tmp/mezmo_out --key \"$MEZMO_API_KEY\""
docker compose cp aura:/tmp/mezmo_out ./mezmo_out
```

(The `aura` container already has outbound access to `mcp.mezmo.com` since
that's the same connection it makes itself.)

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--minutes` | `30` | relative time window |
| `--query` | `""` (broad) | Mezmo fielded query, e.g. `app:checkout-service` |
| `--field` | `app` | field to group/dedup by where a tool takes one |
| `--key` | `$MEZMO_API_KEY` | override the API key |
| `--out-dir` | `.` | where to write the two output files |

## What to actually check once you have the report

1. Open the HTML report. For the dedup/RCA tools, look at the parsed
   table (or raw text if it didn't parse as rows) — does the collapsed
   count look plausible, or suspiciously large?
2. Cross-reference against `raw_export`/`search_logs` for the *same*
   window and query — does the raw line count roughly match what the
   dedup tool implies it collapsed? If dedup claims to represent far more
   lines than actually exist in that window, that's a red flag about the
   matching logic being too aggressive for this domain (the exact failure
   mode we caught and fixed once already for Drain3's `sim_th=0.4`).
3. Check the `tools_discovered` list against what's actually available on
   this account — some tools (the OpenTelemetry trace ones, in
   particular) only appear when Mezmo has recent trace data; an absent
   tool isn't necessarily a bug in this script.
