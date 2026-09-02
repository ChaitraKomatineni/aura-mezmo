# Mezmo query definitions (experimentation only)

Named, reusable query strings for testing `mezmo_deduplicate_logs_*` /
`mezmo_analyze_logs_for_root_cause_*` / etc. against real account data via
`validate_mezmo.py`'s `--query` flag.

**Not wired into `config/aura.toml` yet.** Every real dedup/RCA call so
far has either failed to parse the time value or blown the account's
~1,000,000-line query ceiling (the account produces roughly 195,000
lines/minute unfiltered) -- promoting any of these into what Aura
actually uses would be premature before at least one of them has
returned a real, usable result. These live here as tested definitions
first; promotion to `aura.toml` is a separate, later step.

## Query A

```
host:gen1-prod -app:pickle_rosbridge
```

- **Host filter**: `host:gen1-prod` -- Mezmo's string fields are
  prefix-matched automatically (no wildcard syntax exists, e.g.
  `host:gen1-prod*` is invalid), so this alone matches any host starting
  with those characters (`gen1-prod1`, `gen1-prod22`, etc.) without
  needing to express "followed by a number" explicitly.
- **App exclusion**: `-app:pickle_rosbridge` -- excluded for two reasons
  at once, confirmed: it's high-volume (eats into the row budget) *and*
  considered off-topic for what Query A is meant to answer. Both reasons
  matter for Query B/C later -- a genuine fallback might need to widen
  the host set while still keeping this exclusion for the volume reason,
  even where it re-includes it for the relevance reason. Worth resolving
  explicitly when B gets defined, not assumed from A.

**Not yet verified**: the real distinct `host` values on this account.
`host:gen1-prod`'s prefix match is only as safe as that prefix actually
being unique to the intended hosts -- if a `gen1-prod-canary` or similar
exists, it would silently get pulled in too. Verify with:

```bash
docker run --rm -v "${PWD}/experiments/mezmo-mcp-validate:/app" -w /app python:3.12-slim \
  sh -c "pip install -q fastmcp && python3 validate_mezmo.py --field host --out-dir /app/out --key sts_..."
```

This calls `group_logs_by_field` with `field=host` and returns the real
distinct values with counts -- check the report's `group_by_field`
section before treating `host:gen1-prod` as final.

## Query B, Query C

Not yet defined. Depends on: what Query A's real row count looks like
once tested, and which of the two `pickle_rosbridge` exclusion reasons
(volume vs. relevance) needs to be relaxed for a broader fallback.

## Calibration test: does narrowing to one host help at all?

Before testing the full `host:gen1-prod` prefix (which matches every
`gen1-prodN` host at once), test a single host in isolation to get a
sense of per-host volume against the ~195K lines/minute fleet-wide
figure:

```bash
docker run --rm -v "${PWD}/experiments/mezmo-mcp-validate:/app" -w /app python:3.12-slim \
  sh -c "pip install -q fastmcp && python3 validate_mezmo.py --query 'host:gen1-prod2' --out-dir /app/out --key sts_..."
```

(Use single quotes around the query value, not `\"`-escaped double quotes -- PowerShell
doesn't treat backslash as a quote-escape character, so `\"` passes through literally and
breaks the shell script running inside the container. Single quotes inside a PowerShell
double-quoted string are untouched and `sh` treats them as literal, which is what's needed
once a query has a space in it, e.g. Query A below.)

If a single host's dedup/RCA call *still* hits the "Query Too Large"
ceiling even after halving the time window a few times, the full
multi-host prefix in Query A will need either a much shorter window, an
additional `level:` filter, or both -- worth knowing before assuming
Query A works at any reasonable time window.
