#!/usr/bin/env python3
"""validate_mezmo.py — capture the EXACT raw MCP tool output Mezmo's hosted
server would hand to Aura, for a handful of clustering/dedup-related tools,
and render it into a human-readable report so you can eyeball whether
anything real got collapsed away.

Why this exists: Mezmo's dedup/clustering algorithm is a black box (no
documented similarity threshold, unlike our own hand-tuned Drain3 sim_th).
Before trusting `mezmo_deduplicate_logs_*` / `mezmo_analyze_logs_for_root_cause_*`
for real triage, this script runs them for real against your account and
saves both:
  1. mezmo_raw_output_<timestamp>.json  — the literal MCP CallToolResult
     for every tool call, byte-for-byte what a client (including Aura)
     receives over the wire. Nothing summarized, nothing reshaped.
  2. mezmo_report_<timestamp>.html      — the same data, rendered readable:
     arguments sent, a parsed table where possible, and the full raw JSON
     in a collapsible section per call so nothing is hidden.

IMPORTANT — tool/parameter names are discovered live, not hardcoded from
memory. Mezmo's own docs (docs.mezmo.com/docs/mezmo-mcp) describe tools in
natural-language examples, not raw JSON parameter names, and this script
had no network path to mcp.mezmo.com to confirm the literal schema in
advance. Step 1 below calls `tools/list` first and writes the live schema
to disk (report["tools_schema"]) — that's the ground truth, not anything
guessed here. Each tool call is adaptive: on a "missing field `X`"
deserialize error, it fills in a guess for X and retries (serde-style
errors report one missing field at a time), and for a time-like field it
cycles through several plausible value formats on repeated failures,
since the correct format isn't documented either. Every round's arguments
and outcome are recorded in report["calls"][i]["attempts"], so a wrong
guess is diagnosable rather than silently wrong or swallowed.

Usage:
    pip install -r requirements.txt
    export MEZMO_API_KEY=sts_...        # same key as the repo's .env
    python3 validate_mezmo.py --minutes 30 --query "" --field app

Run this from anywhere that can actually reach mcp.mezmo.com (your host
machine, or `docker compose run --rm aura python3 ...` if you'd rather run
it from inside the stack) — it does NOT need to run inside this repo's
containers, it only needs network access and the API key.
"""
import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
except ImportError:
    sys.exit(
        "Missing dependency. Run: pip install -r requirements.txt\n"
        "(needs fastmcp>=2.0.0, which bundles the MCP client used here)"
    )

MEZMO_URL = "https://mcp.mezmo.com/mcp"

# Name patterns we're looking for among whatever tools/list actually
# returns -- substring match, case-insensitive, so this survives minor
# naming differences between what the docs describe and what the live
# server exposes.
WANTED_TOOLS = {
    "fields": ["list_log_fields"],
    "dedup": ["deduplicate_logs_relative_time", "deduplicate_logs"],
    "root_cause": ["analyze_logs_for_root_cause_relative_time", "root_cause"],
    "group_by_field": ["group_logs_by_field"],
    "histogram": ["get_log_histogram", "histogram"],
    "raw_export": ["export_logs", "search_logs", "export"],
    "correlated_timeline": ["get_correlated_timeline_relative_time", "correlated_timeline"],
}


def find_tool(tools_schema: list[dict], candidates: list[str]) -> dict | None:
    """Best-effort match of a live tool name against our candidate name
    list. Returns the tool's raw schema dict (name + inputSchema) or None
    if nothing matched -- callers must handle that, not assume it exists."""
    names = {t["name"]: t for t in tools_schema}
    for cand in candidates:
        if cand in names:
            return names[cand]
    lowered = {n.lower(): t for n, t in names.items()}
    for cand in candidates:
        for lname, t in lowered.items():
            if cand.lower() in lname:
                return t
    return None


TIME_FIELD_HINTS = ("since", "relative_time", "time_range", "range", "window")
MISSING_FIELD_RE = re.compile(r"missing field `([^`]+)`")


def is_time_field(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in TIME_FIELD_HINTS)


def time_candidates(minutes: int) -> list[str]:
    """Ordered guesses for a relative-time field's value format -- we do
    not know which one a given account/tool actually wants (Mezmo's docs
    describe these tools with natural-language examples, not a raw value
    spec), so call_adaptive() below tries each in turn on a deserialize
    error and records every attempt rather than asserting the first guess
    is correct."""
    return [f"{minutes}m", f"last {minutes} minutes", f"-{minutes}m", f"PT{minutes}M", str(minutes)]


def guess_value(prop_name: str, prop_schema: dict, *, minutes: int, query: str, field: str):
    """Fill one JSON-schema property with a plausible value based on its
    name and declared type. This is a heuristic, not a certainty -- the
    raw output file records exactly what was sent, so a bad guess is
    visible and diagnosable rather than silently wrong."""
    ptype = prop_schema.get("type", "string")
    name = prop_name.lower()

    if "query" in name:
        return query
    if name in ("field", "group_by", "group_by_field", "by"):
        return field
    if "limit" in name:
        return 20 if ptype == "integer" else "20"
    if is_time_field(name):
        return time_candidates(minutes)[0]
    if "minutes" in name:
        return minutes if ptype == "integer" else str(minutes)
    if "mode" in name:
        return "template"
    if "state" in name:
        return None  # let it default; optional in every case we've seen
    if ptype == "boolean":
        return False
    if ptype == "integer" or ptype == "number":
        return minutes
    return ""


def get_input_schema(tool_schema: dict) -> dict:
    """mcp's Tool model's Python field is `input_schema` (JSON alias
    `inputSchema`, used only when dumped with by_alias=True). Check both
    so this survives either serialization mode."""
    return tool_schema.get("input_schema") or tool_schema.get("inputSchema") or {}


def build_arguments(tool_schema: dict, *, minutes: int, query: str, field: str) -> dict:
    """Walk a tool's input schema and fill required properties (plus a few
    obviously-relevant optional ones) with guessed values. Returns the
    arguments dict actually sent -- always saved alongside the result so
    a guess that turns out wrong is diagnosable from the output file."""
    schema = get_input_schema(tool_schema)
    props = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    args = {}
    for prop_name, prop_schema in props.items():
        if prop_name not in required and prop_name.lower() not in (
            "query", "field", "group_by", "group_by_field", "limit", "mode",
        ):
            continue
        value = guess_value(prop_name, prop_schema, minutes=minutes, query=query, field=field)
        if value is not None:
            args[prop_name] = value
    return args


async def call_adaptive(client, tool_name: str, base_args: dict, *, minutes: int, query: str, field: str, max_rounds: int = 8):
    """Call a tool, and on a "missing field `X`" deserialize error, fill in
    a guess for X and retry -- serde-style errors report one missing field
    at a time, so a single guessing pass isn't enough to discover a multi-
    field required schema. For a field whose name looks time-related, cycle
    through time_candidates() on repeated failures instead of giving up
    after one guess, since the correct value FORMAT (not just field name)
    is also unconfirmed. Every round's arguments and outcome are recorded
    and returned, whether or not the call eventually succeeded, so a wrong
    guess is diagnosable rather than silently swallowed."""
    args = dict(base_args)
    time_tries: dict[str, int] = {}
    attempts = []

    for round_no in range(max_rounds):
        try:
            result = await client.call_tool_mcp(tool_name, args)
            attempts.append({"round": round_no, "arguments": dict(args), "outcome": "success"})
            return result, args, attempts
        except Exception as exc:  # noqa: BLE001 -- diagnostic script, every failure mode is recorded, not swallowed
            msg = str(exc)
            attempts.append({"round": round_no, "arguments": dict(args), "outcome": "error", "error": msg})

            m = MISSING_FIELD_RE.search(msg)
            target = m.group(1) if m else None
            if target is None:
                # No explicit field name in the error -- if we already set a
                # time-like field, assume it's the culprit and try the next
                # candidate value for it before giving up.
                target = next((k for k in args if is_time_field(k)), None)

            if target is None:
                break  # nothing left to adjust; stop and report this as the final error

            if is_time_field(target):
                tries = time_tries.get(target, -1) + 1
                cands = time_candidates(minutes)
                if tries >= len(cands):
                    break  # exhausted every guess for this field
                args[target] = cands[tries]
                time_tries[target] = tries
            elif target not in args:
                args[target] = guess_value(target, {}, minutes=minutes, query=query, field=field)
            else:
                break  # already set this field and it's still wrong -- avoid looping forever

    return None, args, attempts


def result_to_plain(mcp_result) -> dict:
    """Dump an mcp CallToolResult (pydantic model) to a plain JSON-safe
    dict, exactly as it came off the wire -- no reshaping."""
    return mcp_result.model_dump(mode="json")


def extract_text_blocks(mcp_result) -> list[str]:
    texts = []
    for block in getattr(mcp_result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    return texts


def try_parse_json_rows(text: str):
    """If a tool's text content is JSON containing a list of flat dicts,
    return that list for table rendering. Otherwise return None -- the
    report falls back to showing the raw text verbatim."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
        return data
    if isinstance(data, dict):
        for key in ("rows", "results", "data", "items", "logs", "groups", "clusters"):
            if isinstance(data.get(key), list) and data[key] and all(isinstance(r, dict) for r in data[key]):
                return data[key]
    return None


async def run(args) -> dict:
    api_key = args.key or os.environ.get("MEZMO_API_KEY", "")
    if not api_key:
        sys.exit("No API key. Pass --key or set MEZMO_API_KEY in your environment.")

    transport = StreamableHttpTransport(
        url=MEZMO_URL,
        headers={"Authorization": f"Bearer {api_key}"},
    )

    report = {
        "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "params": {"minutes": args.minutes, "query": args.query, "field": args.field},
        "tools_discovered": [],
        "tools_schema": [],
        "calls": [],
    }

    async with Client(transport) as client:
        tools_result = await client.list_tools_mcp()
        tools_schema = [t.model_dump(mode="json") for t in tools_result.tools]
        report["tools_discovered"] = [t["name"] for t in tools_schema]
        # Full live schemas, not just names -- ground truth for exact
        # parameter names/types, since Mezmo's docs describe tools in
        # natural-language examples rather than raw JSON schema.
        report["tools_schema"] = tools_schema

        for label, candidates in WANTED_TOOLS.items():
            tool = find_tool(tools_schema, candidates)
            entry = {"label": label, "candidates_tried": candidates}
            if tool is None:
                entry["available"] = False
                entry["note"] = "No matching tool found in this account's live tool list."
                report["calls"].append(entry)
                continue

            entry["available"] = True
            entry["tool_name"] = tool["name"]
            base_args = build_arguments(tool, minutes=args.minutes, query=args.query, field=args.field)

            mcp_result, final_args, attempts = await call_adaptive(
                client, tool["name"], base_args,
                minutes=args.minutes, query=args.query, field=args.field,
            )
            entry["arguments_sent"] = final_args
            entry["attempts"] = attempts
            entry["rounds"] = len(attempts)

            if mcp_result is not None:
                entry["raw_result"] = result_to_plain(mcp_result)
                entry["is_error"] = bool(
                    getattr(mcp_result, "is_error", None)
                    if hasattr(mcp_result, "is_error")
                    else getattr(mcp_result, "isError", False)
                )
                entry["text_blocks"] = extract_text_blocks(mcp_result)
            else:
                entry["is_error"] = True
                entry["exception"] = attempts[-1]["error"] if attempts else "unknown failure"

            report["calls"].append(entry)

    return report


def render_html(report: dict) -> str:
    def esc(s: str) -> str:
        return (
            str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    parts = [
        "<style>",
        "body{font-family:ui-monospace,Menlo,Consolas,monospace;background:#0b0f14;",
        "color:#d7dde3;max-width:980px;margin:32px auto;padding:0 20px;line-height:1.5}",
        "h1{color:#8fd694;font-size:20px} h2{color:#7ec8e3;margin-top:36px;",
        "border-bottom:1px solid #2a3340;padding-bottom:6px}",
        ".meta{color:#8a96a3;font-size:13px;margin-bottom:20px}",
        ".card{background:#111823;border:1px solid #26313e;border-radius:8px;",
        "padding:16px 20px;margin:14px 0}",
        ".ok{color:#8fd694} .err{color:#e38080} .warn{color:#e3c880}",
        "table{border-collapse:collapse;width:100%;margin:10px 0;font-size:12.5px}",
        "th,td{border:1px solid #26313e;padding:5px 9px;text-align:left;vertical-align:top}",
        "th{background:#182230;color:#7ec8e3}",
        "pre{background:#070a0e;border:1px solid #26313e;border-radius:6px;padding:12px;",
        "overflow-x:auto;font-size:12px;white-space:pre-wrap;word-break:break-word}",
        "details summary{cursor:pointer;color:#8a96a3;margin-top:8px}",
        "code{color:#e3b880}",
        "</style>",
        "<h1>Mezmo MCP validation report</h1>",
        f"<div class='meta'>Run at {esc(report['run_at'])} &middot; "
        f"window: last {report['params']['minutes']} minutes &middot; "
        f"query: <code>{esc(report['params']['query'] or '(broad, no filter)')}</code> &middot; "
        f"field: <code>{esc(report['params']['field'])}</code></div>",
        "<div class='card'><b>Tools discovered on this account:</b><br>"
        + ", ".join(f"<code>{esc(n)}</code>" for n in report["tools_discovered"]) + "</div>",
        "<details><summary>Full live tool schemas (ground truth parameter names/types)</summary>"
        f"<pre>{esc(json.dumps(report.get('tools_schema', []), indent=2))}</pre></details>",
    ]

    for call in report["calls"]:
        label = esc(call["label"])
        parts.append(f"<h2>{label}</h2>")
        if not call.get("available"):
            parts.append(
                f"<div class='card warn'>Not available on this account/server. "
                f"Tried: {esc(', '.join(call['candidates_tried']))}</div>"
            )
            continue

        parts.append(
            f"<div class='meta'>tool called: <code>{esc(call['tool_name'])}</code></div>"
        )
        parts.append(
            "<div class='card'><b>Arguments sent</b> (final, after "
            f"{call.get('rounds', 1)} attempt(s))"
            f"<pre>{esc(json.dumps(call.get('arguments_sent', {}), indent=2))}</pre></div>"
        )
        if call.get("rounds", 1) > 1:
            parts.append(
                "<details><summary>Attempt history (each round's arguments and outcome)</summary>"
                f"<pre>{esc(json.dumps(call.get('attempts', []), indent=2))}</pre></details>"
            )

        if call.get("exception"):
            parts.append(f"<div class='card err'><b>Call failed after every guess was exhausted</b><pre>{esc(call['exception'])}</pre></div>")
            continue

        status = "err" if call.get("is_error") else "ok"
        status_label = "ERROR returned by server" if call.get("is_error") else "OK"
        parts.append(f"<div class='card {status}'><b>Status:</b> {status_label}</div>")

        texts = call.get("text_blocks", [])
        rendered_table = False
        for text in texts:
            rows = try_parse_json_rows(text)
            if rows:
                rendered_table = True
                cols = sorted({k for r in rows for k in r.keys()})
                parts.append(f"<div class='card'><b>Parsed result &mdash; {len(rows)} row(s)</b>")
                parts.append("<table><tr>" + "".join(f"<th>{esc(c)}</th>" for c in cols) + "</tr>")
                for r in rows[:200]:
                    parts.append(
                        "<tr>" + "".join(f"<td>{esc(r.get(c, ''))}</td>" for c in cols) + "</tr>"
                    )
                parts.append("</table></div>")

        if not rendered_table and texts:
            parts.append("<div class='card'><b>Raw text content</b>")
            for text in texts:
                parts.append(f"<pre>{esc(text)}</pre>")
            parts.append("</div>")

        parts.append(
            "<details><summary>Full raw MCP result (exact bytes Aura would receive)</summary>"
            f"<pre>{esc(json.dumps(call.get('raw_result', {}), indent=2))}</pre></details>"
        )

    return "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Mezmo MCP validation</title></head><body>" \
        + "".join(parts) + "</body></html>"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--minutes", type=int, default=30, help="relative time window in minutes (default 30)")
    parser.add_argument("--query", default="", help="Mezmo fielded query, e.g. 'app:checkout-service' (default: broad, no filter)")
    parser.add_argument("--field", default="app", help="field to group/dedup by where applicable (default: app)")
    parser.add_argument("--key", default=None, help="Mezmo API key; defaults to $MEZMO_API_KEY")
    parser.add_argument("--out-dir", default=".", help="directory to write output files into")
    args = parser.parse_args()

    try:
        report = asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001 -- top-level: fail with one clear line, not a traceback
        sys.exit(f"Could not complete the run: {type(exc).__name__}: {exc}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    raw_path = out_dir / f"mezmo_raw_output_{stamp}.json"
    raw_path.write_text(json.dumps(report, indent=2))

    html_path = out_dir / f"mezmo_report_{stamp}.html"
    html_path.write_text(render_html(report))

    print(f"Raw MCP output (what Aura would receive):  {raw_path}")
    print(f"Human-readable report:                      {html_path}")


if __name__ == "__main__":
    main()
