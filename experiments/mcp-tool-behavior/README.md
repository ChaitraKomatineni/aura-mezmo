# Mezmo MCP tool behaviour

`probe_tools.py` characterises how each of Mezmo's log tools actually
behaves against a live account: what time range it really uses, exactly what
arguments went over the wire, and what every failure mode literally says.

Separate from `experiments/mezmo-mcp-validate`, which asks "what does the raw
output look like and can I trust the dedup numbers". This one asks "how do
these tools misbehave, and how would I detect it".

## Running it

```bash
pip install -r requirements.txt
export MEZMO_API_KEY=sts_...

python3 probe_tools.py --list                     # show probe groups
python3 probe_tools.py --query "host:gen1-prod" \
    --from 2026-09-17T12:00:00Z --to 2026-09-17T12:20:00Z
python3 probe_tools.py --query "host:gen1-dev9" --minutes 30 --only relative
```

Or through this repo's stack, which already has `fastmcp` and outbound
access to `mcp.mezmo.com`:

```bash
docker compose cp probe_tools.py logs-mcp:/tmp/probe_tools.py
docker compose exec -e MEZMO_API_KEY=$MEZMO_API_KEY logs-mcp \
    python /tmp/probe_tools.py --query host:gen1-prod --minutes 20
```

Every run writes `tool_behavior_<timestamp>.json` with a full record of each
call. Probe groups: `window`, `granularity`, `relative`, `errors`.

Pick a window that has data. Production robots do not log at weekends
(Saturday partial, Sunday zero, Monday full) — probing prod on a Sunday
produces a page of zeroes that look like failures and aren't.

---

## Findings

Measured 2026-09-20 against `host:gen1-prod`, window
`2026-09-17T12:00:00Z → 12:20:00Z` (20 minutes, ~3.49M lines).

### 1. `get_log_histogram` silently widens your window unless you pass `granularity`

`granularity` is optional and "the API will auto-select based on time range".
Auto-select chose **4h** and answered a 20-minute question with 4 hours of data
— while reporting `is_accurate: true`.

| granularity | window actually used | inflation | reported total |
|---|---|---|---|
| *(omitted)* | 12:00 → **16:00** | **12×** | **39,372,907** |
| `30s` | 12:00 → 12:20:30 | 1.02× | 3,494,462 |
| `1m` | 12:00 → 12:21 | 1.05× | 3,494,462 |
| `5m` | 12:00 → 12:25 | 1.25× | 3,494,462 |
| `15m` | 12:00 → 12:30 | 1.5× | 3,494,462 |
| `1h` | 12:00 → 13:00 | 3× | 3,494,462 |
| `4h` | 12:00 → 16:00 | 12× | 39,372,907 |

Two things follow:

- **Always pass an explicit `granularity` finer than your window.** With one
  set, `total` is stable at 3,494,462 across every granularity from 30s to 1h
  — and that figure is independently corroborated (below). The end of the
  window is padded up to the next bucket boundary, but the count is right.
- Omitted (or `4h`) gives a number **11× too large** with no warning.

### 2. The corroborated ground truth

`deduplicate_logs_time_range` refused the same window with:

```
Your query would process 3,494,545 log lines, which exceeds the maximum
limit of 1,000,000.
```

That independently confirms ~3.494M lines for those 20 minutes, matching the
granularity-specified histogram (3,494,462) and **not** the auto-granularity
one (39,372,907).

### 3. `group_logs_by_field` numbers do not reconcile with anything

Same 20-minute window:

| | value | vs true 3.49M |
|---|---|---|
| `total` (field=host) | 7,960,310 | **2.28×** |
| `total` (field=app) | 7,960,310 | **2.28×** |
| bucket sum (field=host) | 7,960,310 | 2.28× — consistent with its own total |
| bucket sum (field=app) | **15,392,363** | **4.4×** — 1.93× its own total |

So there are two independent inflations: the tool's `total` is ~2.28× the real
line count regardless of field, and grouping by `app` double-counts on top of
that (a production line appears to carry more than one app-ish attribution, so
it lands in two buckets; grouping by `host` doesn't, because a line has one host).

**Treat `group_logs_by_field` as ordinal, not cardinal.** Which host or app is
busiest is probably right. Any absolute number it reports is not.

### 4. Relative time: only the documented grammar parses

Grammar is `[last ]<number> <unit>[s][ ago]`, unit ∈ second/minute/hour/day/week.

| works | fails with `Failed to parse relative time` |
|---|---|
| `last 30 minutes`, `30 minutes`, `30 minutes ago`, `last 1 hour`, `1 hour ago` | `30m`, `-30m`, `PT30M`, `30`, `""`, `yesterday`, `last 1 fortnight` |

`last 2 days` parses fine and then fails on volume — a completely different
failure that must not be mistaken for a format problem.

### 5. Hard limits

- **Volume cap: 1,000,000 log lines.** The error states the actual count.
  At fleet volume (~10M lines/hour) `deduplicate_*`, `analyze_logs_for_root_cause_*`
  and `get_correlated_timeline_*` **cannot run on the whole fleet for even 20
  minutes**. They need a narrower query (one robot, a level filter) or a window
  of a few minutes. `get_log_histogram` and `group_logs_by_field` are not capped.
- **Retention: 30 days.** The error names the exact valid range, e.g.
  `(2026-08-21T23:03:07.179Z - 2026-09-20T23:03:07.179Z)`.
- `aggregation` ∈ `count, avg, max, min, sum, p75, p85, p95, p99`.

### 6. Every error arrives as `is_error=true`, not as an exception

MCP allows two failure shapes: a protocol error that raises, and a normal
result carrying `is_error=true`. Measured with `raise_on_error=False`,
**everything below — including serde schema rejections — comes back as
`is_error=true`.** A caller that only wraps the call in `try/except` will treat
every one of these as success. (`validate_mezmo.py` was bitten by exactly this.)

| Label | Trigger | Message |
|---|---|---|
| `SCHEMA_MISSING_FIELD` | omit `since` / `from_time` / `field` | ``failed to deserialize parameters: missing field `since` `` — one field at a time |
| `SCHEMA_BAD_ENUM` | `aggregation: "bogus"` | ``unknown variant `bogus_agg`, expected one of `count`, `avg`, ...`` |
| `SCHEMA_BAD_TYPE` | `limit: "20"` | `invalid type: string "20", expected u32` |
| `SCHEMA_BAD_VALUE` | `limit: -5` | ``invalid value: integer `-5`, expected u32`` |
| `TIME_PARSE_FAILED` | `since: "30m"` | `Failed to parse relative time` |
| `TIME_RANGE_INVERTED` | `to_time` < `from_time` | `'to_time' (...) is earlier than 'from_time' (...)` |
| `TIME_MALFORMED` | `from_time: "17/09/2026 12:00"` | `input contains invalid characters` |
| `BAD_GRANULARITY` | `granularity: "banana"` | real message buried in a serde wrapper: `Serde JSON error: missing field 'meta' ... {"logViewHistogram":{"error":{"status":400,"message":"Error: Invalid granularity: banana."}}}` |
| `VOLUME_REJECTED` | > 1M lines | `**Query Too Large** ... exceeds the maximum limit of 1,000,000` |
| `RETENTION_WINDOW` | 2019 window | `Failed to count logs, timeframe outside of retention period (...)` |
| `TRANSIENT_SSE_DROP` | intermittent | `SSE stream ended without a response` — retry unchanged |
| `TRANSIENT_EMBEDDING` | intermittent, root-cause only | `Embedding step failed: Failed to create OpenAI embeddings for batch` — retry unchanged |

### 7. The dangerous cases: malformed queries succeed and return nothing

These do **not** error. They return a well-formed success with no data, which
is indistinguishable from a genuine "no logs matched":

| Input | Result |
|---|---|
| `query: "(host:gen1-prod"` (unbalanced paren) | `OK, total=0` |
| `query: "host:gen1-prod*"` (banned `*` wildcard) | `OK, total=0` |
| `field: "definitely_not_a_field"` | `OK, total=7,960,310, buckets=0` |

An agent handed any of these will confidently report that nothing happened.
The harness flags these as `SILENT EMPTIES`; nothing in the protocol does.
Note the second one is the exact mistake `host:gen1-prod*` would have been —
it fails silently rather than loudly.

---

## What this implies for `services/mezmo-proxy`

1. **Inject a `granularity`** into `get_log_histogram` when the caller omits
   one, or drop the tool. As-is it reports 11×-inflated counts as accurate.
2. **The 1M cap is the real constraint** on the three heavyweight tools. The
   proxy's existing scope (`-level:debug`, plus INFO suppression) cuts volume
   by roughly an order of magnitude, which is what makes them usable at all —
   that filter is doing more work than noise reduction.
3. **Treat an empty result as suspect.** The proxy could check the caller's
   query for unbalanced parens and `*` outside `field:*` and reject loudly,
   rather than letting Mezmo return a confident zero.
4. **`group_logs_by_field` counts should not be quoted as line counts** by the
   agent — ordinal only.
