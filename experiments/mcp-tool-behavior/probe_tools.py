#!/usr/bin/env python3
"""probe_tools.py -- characterise how each Mezmo MCP log tool actually
BEHAVES, against a live account.

This is deliberately separate from experiments/mezmo-mcp-validate, which
answers "what does the raw output look like, and can I trust the dedup
numbers". This one answers different questions:

  1. What time range does each tool ACTUALLY use? Several of them echo a
     window back (`from_ms`/`to_ms`, `time_range`) that is not the window
     you asked for -- get_log_histogram in particular snaps out to its
     auto-selected bucket granularity, so a 1-hour request can be answered
     with 4 hours of data while still reporting is_accurate: true.
  2. Exactly what arguments went over the wire, for every attempt.
  3. What errors exist, what they literally say, and which of the two MCP
     failure shapes they arrive as. That distinction matters and is easy to
     get wrong:
        - PROTOCOL error: the call raises before the tool runs (schema
          validation, e.g. "missing field `since`").
        - APPLICATION error: a perfectly normal CallToolResult that happens
          to carry is_error=true and an explanatory text block (e.g.
          "Failed to parse relative time").
     Treating the second as success is a real bug that has already bitten
     this repo once.

Mezmo does not publish an error catalogue -- docs.mezmo.com/docs/mezmo-mcp
describes query protection and retention limits in prose but lists no
codes or strings. So ERROR_SIGNATURES below is an OBSERVED taxonomy, built
from real responses. Anything unmatched is reported as UNCLASSIFIED rather
than silently bucketed, so new failure modes surface instead of hiding.

Usage:
    pip install -r requirements.txt
    export MEZMO_API_KEY=sts_...
    python3 probe_tools.py --query "host:gen1-prod" \
        --from 2026-09-17T12:00:00Z --to 2026-09-17T13:00:00Z

    # or against whatever is live right now:
    python3 probe_tools.py --query "host:gen1-dev9" --minutes 30

    python3 probe_tools.py --only window,granularity   # run some groups
    python3 probe_tools.py --list                      # show probe groups

Run it from anywhere that can reach mcp.mezmo.com. Inside this repo's
stack works too:
    docker compose cp probe_tools.py logs-mcp:/tmp/probe_tools.py
    docker compose exec -e MEZMO_API_KEY=$MEZMO_API_KEY logs-mcp \
        python /tmp/probe_tools.py --query host:gen1-dev9 --minutes 30
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
except ImportError:
    sys.exit("Missing dependency. Run: pip install -r requirements.txt")

MEZMO_URL = os.environ.get("MEZMO_MCP_URL", "https://mcp.mezmo.com/mcp")
RFC3339 = "%Y-%m-%dT%H:%M:%SZ"

# The eight log-reading tools under test. Pipeline management, AI
# investigations and the trace tools are out of scope here.
LOG_TOOLS = [
    "get_log_histogram",
    "group_logs_by_field",
    "deduplicate_logs_relative_time",
    "deduplicate_logs_time_range",
    "analyze_logs_for_root_cause_relative_time",
    "analyze_logs_for_root_cause_time_range",
    "get_correlated_timeline_relative_time",
    "get_correlated_timeline_time_range",
]

# ---------------------------------------------------------------------------
# Observed error taxonomy. (pattern, label, shape, note)
#
# On `shape`: MCP allows two failure shapes -- a protocol error that raises
# before the tool runs, and a normal CallToolResult carrying is_error=true.
# IMPORTANT, measured: this harness calls with raise_on_error=False, and
# with that setting EVERY failure below -- including serde schema
# rejections -- comes back as is_error=true rather than raising. So the
# shape you observe is a property of YOUR CLIENT SETTING, not of Mezmo.
# The practical consequence for any caller: you cannot detect failure by
# catching exceptions alone. A caller that only wraps the call in
# try/except will silently treat every error in this table as success.
# (validate_mezmo.py was bitten by exactly this.) `shape` below records
# what was actually observed under raise_on_error=False.
# ---------------------------------------------------------------------------
ERROR_SIGNATURES = [
    (r"failed to deserialize parameters: missing field `(?P<f>[^`]+)`",
     "SCHEMA_MISSING_FIELD", "application",
     "Required parameter absent. serde reports ONE field at a time, so a "
     "multi-required-field schema needs several round trips to discover."),
    (r"unknown variant .*expected",
     "SCHEMA_BAD_ENUM", "application",
     "Value not in the allowed set; the message lists the valid options. "
     "Observed: aggregation is count/avg/max/min/sum/p75/p85/p95/p99, but "
     "dedup_mode is only `none` or `template` -- NOT `exact`, despite the "
     "schema leaving dedup_mode untyped."),
    (r"invalid type: \w+ .*expected",
     "SCHEMA_BAD_TYPE", "application",
     'Wrong JSON type. Two distinct causes seen: limit as the string "20" '
     "when u32 is required; and -- the one that will catch you -- every "
     'aggregation EXCEPT count must be an object naming a field, e.g. '
     '{"avg": "latency_ms"}. Passing bare "avg" is a type error, even '
     'though bare "count" is accepted.'),
    (r"invalid value: .*expected",
     "SCHEMA_BAD_VALUE", "application",
     "Right type, impossible value -- e.g. limit: -5 against u32."),
    (r"failed to parse relative time",
     "TIME_PARSE_FAILED", "application",
     "The `since` value did not match `[last ]<n> <unit>[s][ ago]`. Measured "
     "failures: 30m, -30m, PT30M, 30, '', yesterday, 'last 1 fortnight'."),
    (r"is earlier than 'from_time'|to_time.*earlier",
     "TIME_RANGE_INVERTED", "application",
     "to_time precedes from_time. Clear message, names both values."),
    (r"input contains invalid characters",
     "TIME_MALFORMED", "application",
     "Timestamp not RFC3339 (e.g. '17/09/2026 12:00', or the literal 'now')."),
    (r"invalid granularity",
     "BAD_GRANULARITY", "application",
     "granularity must look like 30s/1m/5m/15m/1h/4h. Note the real message "
     "is buried inside a serde wrapper: 'Serde JSON error: missing field "
     "`meta` ... {\"logViewHistogram\":{\"error\":{...}}}'."),
    (r"query too large|exceeds the maximum limit",
     "VOLUME_REJECTED", "application",
     "Query protection. HARD CAP 1,000,000 log lines; the message states the "
     "actual count it would have processed. The time FORMAT was fine -- this "
     "is a volume problem, so narrow the window/query, do not switch format."),
    (r"retention|timeframe outside|valid time range",
     "RETENTION_WINDOW", "application",
     "Window outside retention. Measured retention: 30 days, and the error "
     "states the exact valid range."),
    (r"sse stream ended without a response",
     "TRANSIENT_SSE_DROP", "protocol",
     "Upstream hiccup, not a bad request. Retry the SAME arguments."),
    (r"embedding step failed|failed to create .*embeddings",
     "TRANSIENT_EMBEDDING", "application",
     "Root-cause analysis embeds log text; this is its backend failing. "
     "Retry unchanged."),
    (r"unknown tool|tool not found",
     "UNKNOWN_TOOL", "protocol",
     "Not exposed on this account (or filtered out by a proxy)."),
    (r"unauthor|forbidden|invalid.*(api key|token)|401|403",
     "AUTH", "protocol", "Bad or missing MEZMO_API_KEY."),
    (r"timed? ?out|deadline",
     "TIMEOUT", "protocol", "Call exceeded the client or server deadline."),
]

# Cases that are NOT errors but should be, and are the most dangerous
# results here: a malformed query returns HTTP-200-shaped success with
# nothing in it, which reads as "no logs matched" rather than "your query
# was wrong". Measured: an unbalanced paren and a banned `*` wildcard both
# return total=0, and an unknown field name returns a correct total with
# zero buckets. Anything an agent does with those is confidently wrong.
SILENT_FAILURE_NOTE = (
    "returned success with no data -- check whether the QUERY was actually "
    "valid; Mezmo does not reject malformed queries, it returns empty"
)


def classify(text: str):
    low = (text or "").lower()
    for pattern, label, shape, note in ERROR_SIGNATURES:
        if re.search(pattern, low):
            return label, shape, note
    return "UNCLASSIFIED", "?", "Not in the observed taxonomy -- add it."


# ---------------------------------------------------------------------------
# Response introspection: what window did the tool actually use?
# ---------------------------------------------------------------------------
WINDOW_KEYS = [("from_ms", "to_ms"), ("from_time", "to_time"), ("start", "end")]


def _to_dt(value):
    """Mezmo is inconsistent here: histogram/group_by echo ISO strings in
    fields NAMED *_ms, while correlated_timeline uses real epoch ms ints."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def extract_window(payload):
    """Return (start, end) the response claims it covered, or (None, None)."""
    if not isinstance(payload, dict):
        return None, None
    for container in (payload, payload.get("time_range") or {}):
        if not isinstance(container, dict):
            continue
        for a, b in WINDOW_KEYS:
            if a in container and b in container:
                return _to_dt(container[a]), _to_dt(container[b])
    return None, None


def payload_of(result):
    """First JSON-parseable view of a result, else None."""
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def result_text(result):
    return " ".join(
        b.text for b in (getattr(result, "content", None) or [])
        if getattr(b, "text", None)
    ).strip()


# ---------------------------------------------------------------------------
# One call, fully recorded.
# ---------------------------------------------------------------------------
async def probe(client, group, label, tool, args, requested=None):
    record = {
        "group": group, "label": label, "tool": tool,
        "arguments_sent": args, "requested_window": None,
    }
    if requested:
        record["requested_window"] = [requested[0].strftime(RFC3339),
                                      requested[1].strftime(RFC3339)]

    started = time.monotonic()
    try:
        result = await client.call_tool(tool, args, raise_on_error=False)
        shape = "application" if getattr(result, "is_error", False) else "ok"
        text = result_text(result)
    except Exception as exc:  # noqa: BLE001 -- every failure mode is data here
        record.update(elapsed_s=round(time.monotonic() - started, 2),
                      outcome="error", shape="protocol",
                      error=f"{type(exc).__name__}: {exc}")
        label_, expected_shape, note = classify(record["error"])
        record.update(error_label=label_, expected_shape=expected_shape, note=note)
        return record

    record["elapsed_s"] = round(time.monotonic() - started, 2)

    if shape == "application":
        record.update(outcome="error", shape="application", error=text)
        label_, expected_shape, note = classify(text)
        record.update(error_label=label_, expected_shape=expected_shape, note=note,
                      shape_matches_expectation=(expected_shape == "application"))
        return record

    record["outcome"] = "ok"
    record["shape"] = "ok"
    payload = payload_of(result)
    record["response_chars"] = len(text)

    start, end = extract_window(payload)
    if start and end:
        record["actual_window"] = [start.strftime(RFC3339), end.strftime(RFC3339)]
        record["actual_minutes"] = round((end - start).total_seconds() / 60, 1)
        if requested:
            req_min = (requested[1] - requested[0]).total_seconds() / 60
            record["requested_minutes"] = round(req_min, 1)
            record["window_inflation"] = (
                round(record["actual_minutes"] / req_min, 2) if req_min else None
            )
    if isinstance(payload, dict):
        for k in ("total", "interval", "granularity", "is_accurate", "dedup_mode",
                  "grouping_field"):
            if k in payload:
                record[k] = payload[k]
        if isinstance(payload.get("buckets"), list):
            buckets = payload["buckets"]
            record["bucket_count"] = len(buckets)
            record["bucket_sum"] = sum(b.get("count", 0) for b in buckets)
            if payload.get("total"):
                record["bucket_sum_vs_total"] = round(
                    record["bucket_sum"] / payload["total"], 3)
        if isinstance(payload.get("stats"), dict):
            record["stats"] = payload["stats"]

    # Flag the silent-failure shape: succeeded, but returned nothing.
    empty_total = record.get("total") == 0
    empty_buckets = record.get("bucket_count") == 0 and "bucket_count" in record
    empty_stats = (record.get("stats") or {}).get("total_logs_fetched") == 0
    if empty_total or empty_buckets or empty_stats:
        record["silent_empty"] = True
        record["note"] = SILENT_FAILURE_NOTE
    return record


# ---------------------------------------------------------------------------
# Probe groups. Each returns a list of (label, tool, args, requested_window).
# ---------------------------------------------------------------------------
def group_window(q, start, end):
    """Does each absolute-time tool honour the window it was given?"""
    w = {"from_time": start.strftime(RFC3339), "to_time": end.strftime(RFC3339)}
    return [
        ("histogram, no granularity", "get_log_histogram", {"query": q, **w}, (start, end)),
        ("group_by app", "group_logs_by_field",
         {"query": q, "field": "app", "aggregation": "count", "limit": 100, **w}, (start, end)),
        ("group_by host", "group_logs_by_field",
         {"query": q, "field": "host", "aggregation": "count", "limit": 100, **w}, (start, end)),
        ("dedup time_range", "deduplicate_logs_time_range", {"query": q, **w}, (start, end)),
        ("root_cause time_range", "analyze_logs_for_root_cause_time_range",
         {"query": q, **w}, (start, end)),
        ("timeline time_range", "get_correlated_timeline_time_range",
         {"query": q, **w}, (start, end)),
    ]


def group_granularity(q, start, end):
    """Does an explicit granularity stop the histogram widening the window?"""
    w = {"from_time": start.strftime(RFC3339), "to_time": end.strftime(RFC3339)}
    probes = [("granularity omitted", "get_log_histogram", {"query": q, **w}, (start, end))]
    for g in ("30s", "1m", "5m", "15m", "1h", "4h"):
        probes.append((f"granularity={g}", "get_log_histogram",
                       {"query": q, "granularity": g, **w}, (start, end)))
    probes.append(("granularity=bogus", "get_log_histogram",
                   {"query": q, "granularity": "banana", **w}, (start, end)))
    return probes


def group_relative(q, minutes):
    """Which `since` spellings parse? Schema documents
    `[last ]<n> <unit>[s][ ago]`; everything else is expected to fail."""
    candidates = [
        f"last {minutes} minutes",     # documented
        f"{minutes} minutes",          # documented
        f"{minutes} minutes ago",      # documented
        "last 1 hour", "1 hour ago", "last 2 days",
        f"{minutes}m",                 # expected to fail
        f"-{minutes}m", f"PT{minutes}M", str(minutes),
        "", "yesterday", "last 1 fortnight",
    ]
    return [(f"since={c!r}", "get_correlated_timeline_relative_time",
             {"query": q, "since": c, "max_logs_per_source": 1,
              "max_timeline_events": 1}, None)
            for c in candidates]


def group_errors(q, start, end):
    """Deliberately provoke each failure mode we can reach safely."""
    w = {"from_time": start.strftime(RFC3339), "to_time": end.strftime(RFC3339)}
    old = datetime(2019, 1, 1, tzinfo=timezone.utc)
    return [
        ("missing required `since`", "deduplicate_logs_relative_time", {"query": q}, None),
        ("missing from_time/to_time", "deduplicate_logs_time_range", {"query": q}, None),
        ("missing field+aggregation", "group_logs_by_field", {"query": q}, None),
        ("bad aggregation enum", "group_logs_by_field",
         {"query": q, "field": "app", "aggregation": "bogus_agg", **w}, None),
        ("limit as string", "group_logs_by_field",
         {"query": q, "field": "app", "aggregation": "count", "limit": "20", **w}, None),
        ("inverted range (to<from)", "get_log_histogram",
         {"query": q, "from_time": end.strftime(RFC3339),
          "to_time": start.strftime(RFC3339)}, None),
        ("beyond retention (2019)", "get_log_histogram",
         {"query": q, "from_time": old.strftime(RFC3339),
          "to_time": (old + timedelta(hours=1)).strftime(RFC3339)}, None),
        ("malformed timestamp", "get_log_histogram",
         {"query": q, "from_time": "17/09/2026 12:00", "to_time": "now"}, None),
        ("unknown field name", "group_logs_by_field",
         {"query": q, "field": "definitely_not_a_field", "aggregation": "count", **w}, None),
        ("negative limit", "group_logs_by_field",
         {"query": q, "field": "app", "aggregation": "count", "limit": -5, **w}, None),
        ("unbalanced parens in query", "get_log_histogram",
         {"query": "(host:gen1-prod", **w}, None),
        ("banned wildcard in value", "get_log_histogram",
         {"query": "host:gen1-prod*", **w}, None),
        ("huge volume, 30 days", "deduplicate_logs_relative_time",
         {"query": "", "since": "last 30 days"}, None),
    ]


def group_volume(q, start, end):
    """How big a window can each tool actually take before the 1,000,000
    line cap rejects it? Windows all END at `end` and grow backwards, so
    every rung sees the same (populated) tail of data."""
    rungs = [1, 2, 5, 10, 20, 60, 240]
    capped = [
        ("dedup", "deduplicate_logs_time_range", {}),
        ("root_cause", "analyze_logs_for_root_cause_time_range", {}),
        ("timeline", "get_correlated_timeline_time_range", {}),
        # Not believed to be capped -- included to prove the contrast.
        ("histogram", "get_log_histogram", {"granularity": "1m"}),
        ("group_by", "group_logs_by_field", {"field": "host", "aggregation": "count"}),
    ]
    probes = []
    for minutes in rungs:
        w_start = end - timedelta(minutes=minutes)
        w = {"from_time": w_start.strftime(RFC3339), "to_time": end.strftime(RFC3339)}
        for name, tool, extra in capped:
            probes.append((f"{name} @ {minutes}min", tool,
                           {"query": q, **extra, **w}, (w_start, end)))
    return probes


def group_matrix(q, start, end):
    """Sweep each tool's own parameters, so the report can say what every
    knob actually does. Windows are kept small to stay under the volume cap
    where the tool is subject to it."""
    tight_start = end - timedelta(minutes=2)
    tight = {"from_time": tight_start.strftime(RFC3339), "to_time": end.strftime(RFC3339)}
    w = {"from_time": start.strftime(RFC3339), "to_time": end.strftime(RFC3339)}
    probes = []

    # group_logs_by_field: every aggregation, several fields, limit behaviour
    for agg in ("count", "avg", "max", "min", "sum", "p95"):
        args = {"query": q, "field": "app", "aggregation": agg, **w}
        probes.append((f"aggregation={agg}", "group_logs_by_field", args, (start, end)))
    for field in ("host", "app", "level", "_app", "pod"):
        probes.append((f"field={field}", "group_logs_by_field",
                       {"query": q, "field": field, "aggregation": "count", **w}, (start, end)))
    for lim in (0, 1, 5, 1000):
        probes.append((f"limit={lim}", "group_logs_by_field",
                       {"query": q, "field": "app", "aggregation": "count",
                        "limit": lim, **w}, (start, end)))

    # get_log_histogram: chart flag, and defaults when times omitted
    probes.append(("include_chart=true", "get_log_histogram",
                   {"query": q, "granularity": "5m", "include_chart": True, **w}, (start, end)))
    probes.append(("no from/to (defaults)", "get_log_histogram",
                   {"query": q, "granularity": "5m"}, None))

    # correlated timeline: grouping field, dedup mode, the two caps
    for gf in ("_app", "_host", "app"):
        probes.append((f"grouping_field={gf}", "get_correlated_timeline_time_range",
                       {"query": q, "grouping_field": gf, "max_logs_per_source": 3, **tight},
                       (tight_start, end)))
    for mode in ("template", "exact", "none"):
        probes.append((f"dedup_mode={mode}", "get_correlated_timeline_time_range",
                       {"query": q, "dedup_mode": mode, "max_logs_per_source": 3, **tight},
                       (tight_start, end)))
    for n in (1, 5, 100):
        probes.append((f"max_logs_per_source={n}", "get_correlated_timeline_time_range",
                       {"query": q, "max_logs_per_source": n, **tight}, (tight_start, end)))

    # root cause: does incident_description change anything?
    probes.append(("root_cause plain", "analyze_logs_for_root_cause_time_range",
                   {"query": q, **tight}, (tight_start, end)))
    probes.append(("root_cause w/ description", "analyze_logs_for_root_cause_time_range",
                   {"query": q, "incident_description": "conveyor stopped unexpectedly",
                    **tight}, (tight_start, end)))

    # dedup, both time flavours, on a window small enough to pass
    probes.append(("dedup time_range tight", "deduplicate_logs_time_range",
                   {"query": q, **tight}, (tight_start, end)))
    probes.append(("dedup relative", "deduplicate_logs_relative_time",
                   {"query": q, "since": "last 2 minutes"}, None))
    return probes


GROUPS = {
    "window": ("Does each tool honour the requested time window?", group_window),
    "granularity": ("Does explicit granularity stop histogram window inflation?", group_granularity),
    "relative": ("Which `since` spellings parse?", group_relative),
    "errors": ("Provoke and label every reachable failure mode", group_errors),
    "volume": ("How large a window does each tool accept before the 1M cap?", group_volume),
    "matrix": ("Sweep every tool's own parameters", group_matrix),
}


# ---------------------------------------------------------------------------
def render(records):
    by_group = {}
    for r in records:
        by_group.setdefault(r["group"], []).append(r)

    for group, rows in by_group.items():
        print("\n" + "=" * 100)
        print(f"GROUP: {group} -- {GROUPS[group][0]}")
        print("=" * 100)
        for r in rows:
            head = f"{r['label']}"
            print(f"\n  {head}")
            print(f"    tool     : {r['tool']}")
            print(f"    args     : {json.dumps(r['arguments_sent'])[:180]}")
            if r["outcome"] == "ok":
                bits = [f"{r.get('elapsed_s')}s", f"{r.get('response_chars', 0):,} chars"]
                if r.get("total") is not None:
                    bits.append(f"total={r['total']:,}")
                if r.get("bucket_sum") is not None:
                    bits.append(f"buckets={r['bucket_sum']:,} "
                                f"({r.get('bucket_sum_vs_total')}x total)")
                if r.get("stats"):
                    bits.append(f"stats={json.dumps(r['stats'])}")
                print(f"    OK       : {'  |  '.join(bits)}")
                if r.get("actual_window"):
                    inflation = r.get("window_inflation")
                    warn = ""
                    if inflation and inflation > 1.01:
                        warn = f"   <-- WIDENED {inflation}x"
                    print(f"    window   : requested {r.get('requested_minutes')}min "
                          f"-> actual {r.get('actual_minutes')}min{warn}")
                    print(f"               {r['actual_window'][0]} .. {r['actual_window'][1]}")
                for k in ("interval", "granularity", "is_accurate", "dedup_mode"):
                    if r.get(k) is not None:
                        print(f"    {k:9}: {r[k]}")
                if r.get("silent_empty"):
                    print(f"    SILENT   : {r['note']}")
            else:
                mism = ""
                if r.get("shape_matches_expectation") is False:
                    mism = f"  (!! arrived as {r['shape']}, taxonomy expected " \
                           f"{r['expected_shape']})"
                print(f"    {r['error_label']:<22} [{r['shape']}]{mism}")
                print(f"    message  : {r['error'][:300]}")
                print(f"    why      : {r['note']}")

    print("\n" + "=" * 100)
    print("ERROR SUMMARY")
    print("=" * 100)
    errs = [r for r in records if r["outcome"] == "error"]
    if not errs:
        print("  (no errors)")
    seen = {}
    for r in errs:
        seen.setdefault((r["error_label"], r["shape"]), []).append(r["label"])
    for (lab, shape), labels in sorted(seen.items()):
        print(f"  {lab:<22} [{shape:<11}] x{len(labels):<3} {', '.join(labels[:4])}")
    unclassified = [r for r in errs if r["error_label"] == "UNCLASSIFIED"]
    if unclassified:
        print("\n  UNCLASSIFIED -- add these to ERROR_SIGNATURES:")
        for r in unclassified:
            print(f"    [{r['shape']}] {r['error'][:200]}")

    silent = [r for r in records if r.get("silent_empty")]
    if silent:
        print("\n  SILENT EMPTIES -- succeeded but returned nothing. Each of these "
              "\n  is indistinguishable from a genuine 'no logs matched':")
        for r in silent:
            print(f"    {r['tool']:<42} {r['label']}")


async def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--query", default="host:gen1-prod", help="Mezmo query to probe with")
    ap.add_argument("--from", dest="from_time", help="RFC3339 window start")
    ap.add_argument("--to", dest="to_time", help="RFC3339 window end")
    ap.add_argument("--minutes", type=int, default=20,
                    help="if --from/--to omitted, use the last N minutes (default 20)")
    ap.add_argument("--only", help="comma-separated groups: " + ",".join(GROUPS))
    ap.add_argument("--list", action="store_true", help="list probe groups and exit")
    ap.add_argument("--key", default=None)
    ap.add_argument("--out-dir", default=".")
    args = ap.parse_args()

    if args.list:
        for name, (desc, _) in GROUPS.items():
            print(f"  {name:<12} {desc}")
        return 0

    key = args.key or os.environ.get("MEZMO_API_KEY", "")
    if not key:
        sys.exit("No API key. Pass --key or set MEZMO_API_KEY.")

    if args.from_time and args.to_time:
        start = datetime.fromisoformat(args.from_time.replace("Z", "+00:00"))
        end = datetime.fromisoformat(args.to_time.replace("Z", "+00:00"))
    else:
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = end - timedelta(minutes=args.minutes)

    chosen = [g.strip() for g in args.only.split(",")] if args.only else list(GROUPS)
    bad = [g for g in chosen if g not in GROUPS]
    if bad:
        sys.exit(f"Unknown group(s): {bad}. Available: {list(GROUPS)}")

    print(f"target query : {args.query!r}")
    print(f"window       : {start.strftime(RFC3339)} -> {end.strftime(RFC3339)} "
          f"({(end-start).total_seconds()/60:.0f} min)")
    print(f"groups       : {chosen}")

    transport = StreamableHttpTransport(url=MEZMO_URL,
                                        headers={"Authorization": f"Bearer {key}"})
    records = []
    schemas = {}
    async with Client(transport) as client:
        live_tools = await client.list_tools()
        live = {t.name for t in live_tools}
        # Captured so make_report.py can document each tool's real
        # description, parameters, defaults and enums without a second
        # network round trip.
        for t in live_tools:
            if t.name in LOG_TOOLS:
                schemas[t.name] = {
                    "description": t.description,
                    "input_schema": (getattr(t, "input_schema", None)
                                     or getattr(t, "inputSchema", None) or {}),
                }
        missing = [t for t in LOG_TOOLS if t not in live]
        if missing:
            print(f"NOTE: not available on this account/endpoint: {missing}")

        for name in chosen:
            builder = GROUPS[name][1]
            specs = (builder(args.query, args.minutes) if name == "relative"
                     else builder(args.query, start, end))
            for label, tool, call_args, requested in specs:
                if tool not in live:
                    records.append({"group": name, "label": label, "tool": tool,
                                    "arguments_sent": call_args, "outcome": "error",
                                    "shape": "protocol", "error": f"Unknown tool: {tool}",
                                    "error_label": "UNKNOWN_TOOL",
                                    "expected_shape": "protocol",
                                    "note": "Not exposed here."})
                    continue
                records.append(await probe(client, name, label, tool, call_args, requested))

    render(records)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"tool_behavior_{stamp}.json"
    path.write_text(json.dumps(
        {"run_at": stamp, "url": MEZMO_URL, "query": args.query,
         "window": [start.strftime(RFC3339), end.strftime(RFC3339)],
         "groups": chosen, "schemas": schemas, "records": records},
        indent=2, default=str))
    print(f"\nrecords written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
