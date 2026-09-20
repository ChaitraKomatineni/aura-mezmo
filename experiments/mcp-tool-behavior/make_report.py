#!/usr/bin/env python3
"""make_report.py -- turn probe_tools.py's JSON records into a per-tool
reference document.

probe_tools.py COLLECTS (one JSON per run, organised by probe group).
This SUMMARISES (one Markdown doc, organised by tool), which is the shape
you actually want when the question is "how do I use this tool and what
will it do to me".

It reads every tool_behavior_*.json it can find and merges them, so you can
build the report from several runs -- different queries, different windows,
weekday vs weekend -- rather than being limited to one. Later runs win on
schema; observations accumulate.

Usage:
    python3 make_report.py                          # ./out/*.json -> TOOL_REPORT.md
    python3 make_report.py --in out --out TOOL_REPORT.md
    python3 make_report.py --in out/tool_behavior_20260920T230504Z.json
"""

import argparse
import json
import re
import textwrap
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

TOOL_ORDER = [
    "get_log_histogram",
    "group_logs_by_field",
    "deduplicate_logs_relative_time",
    "deduplicate_logs_time_range",
    "analyze_logs_for_root_cause_relative_time",
    "analyze_logs_for_root_cause_time_range",
    "get_correlated_timeline_relative_time",
    "get_correlated_timeline_time_range",
]


def load(paths):
    runs = []
    for p in paths:
        try:
            runs.append((p, json.loads(Path(p).read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  skipping {p}: {e}")
    runs.sort(key=lambda r: r[1].get("run_at", ""))
    return runs


def one_line(s, n=400):
    return " ".join(str(s or "").split())[:n]


def fmt_args(args):
    return json.dumps(args, sort_keys=True)


def describe_params(schema):
    """Markdown bullet list of a tool's parameters with types/defaults."""
    props = (schema.get("properties") or {})
    required = set(schema.get("required") or [])
    lines = []
    for name, spec in props.items():
        typ = spec.get("type")
        if isinstance(typ, list):
            typ = "/".join(t for t in typ if t != "null") + "?"
        bits = [f"`{name}`", f"({typ or 'any'})"]
        if name in required:
            bits.append("**required**")
        desc = one_line(spec.get("description"), 240)
        # The `query` description is Mezmo's entire 2.2k-char syntax guide;
        # summarising it here would just be a worse copy of their docs.
        if name == "query":
            desc = ("Mezmo query string. Full syntax guide lives in the live "
                    "schema (~2.2k chars): fielded search, implicit AND, "
                    "explicit OR, `-` negation, parentheses, numeric "
                    "comparisons. Automatic prefix matching; `*` is valid "
                    "ONLY as `field:*`.")
        extra = []
        if "default" in spec:
            extra.append(f"default `{spec['default']}`")
        if "enum" in spec:
            extra.append("one of " + ", ".join(f"`{e}`" for e in spec["enum"]))
        if "minimum" in spec:
            extra.append(f"min {spec['minimum']}")
        suffix = f" _({'; '.join(extra)})_" if extra else ""
        lines.append(f"- {' '.join(bits)}{suffix}" + (f" — {desc}" if desc else ""))
    return lines or ["- _(no parameters)_"]


def volume_ceiling(records, tool):
    """From the `volume` group: largest window that succeeded, smallest
    that was rejected for volume."""
    ok, rejected = [], []
    for r in records:
        if r["tool"] != tool or r["group"] != "volume":
            continue
        m = re.search(r"@ (\d+)min", r["label"])
        if not m:
            continue
        minutes = int(m.group(1))
        if r["outcome"] == "ok":
            ok.append(minutes)
        elif r.get("error_label") == "VOLUME_REJECTED":
            rejected.append((minutes, r.get("error", "")))
    if not ok and not rejected:
        return None
    biggest_ok = max(ok) if ok else None
    smallest_bad = min(m for m, _ in rejected) if rejected else None
    line_counts = []
    for _, msg in rejected:
        m = re.search(r"process \*\*([\d,]+) log lines", msg) or \
            re.search(r"process ([\d,]+) log lines", msg)
        if m:
            line_counts.append(m.group(1))
    return {"largest_ok_min": biggest_ok, "smallest_rejected_min": smallest_bad,
            "counts_seen": line_counts}


def render_tool(tool, schema, records):
    out = [f"\n## `{tool}`\n"]
    if schema and schema.get("description"):
        out.append("> " + one_line(schema["description"], 600) + "\n")

    mine = [r for r in records if r["tool"] == tool]
    if not mine:
        out.append("_Not exercised in any loaded run._\n")
        return out

    if schema:
        out.append("**Parameters**\n")
        out += describe_params(schema.get("input_schema") or {})
        out.append("")

    ok = [r for r in mine if r["outcome"] == "ok"]
    errs = [r for r in mine if r["outcome"] == "error"]
    silent = [r for r in mine if r.get("silent_empty")]

    out.append(f"**Observed:** {len(mine)} calls — {len(ok)} ok, {len(errs)} errors, "
               f"{len(silent)} succeeded-but-empty.\n")

    # --- a verified working invocation -------------------------------------
    good = [r for r in ok if not r.get("silent_empty")]
    if good:
        best = max(good, key=lambda r: r.get("response_chars", 0))
        out.append("**A call that worked**\n")
        out.append("```json")
        out.append(json.dumps(best["arguments_sent"], indent=2))
        out.append("```")
        bits = []
        if best.get("total") is not None:
            bits.append(f"`total` {best['total']:,}")
        if best.get("stats"):
            bits.append(f"stats `{json.dumps(best['stats'])}`")
        bits.append(f"{best.get('response_chars', 0):,} chars")
        bits.append(f"{best.get('elapsed_s')}s")
        out.append("→ " + ", ".join(bits) + "\n")

    # --- window fidelity ----------------------------------------------------
    widened = [r for r in ok if (r.get("window_inflation") or 1) > 1.01]
    if widened:
        out.append("**Time-window fidelity — this tool does not always use the "
                   "window you asked for**\n")
        out.append("| call | requested | actually used | inflation |")
        out.append("|---|---|---|---|")
        for r in sorted(widened, key=lambda x: -(x.get("window_inflation") or 0))[:10]:
            out.append(f"| {r['label']} | {r.get('requested_minutes')} min "
                       f"| {r.get('actual_minutes')} min | **{r['window_inflation']}×** |")
        out.append("")
    elif any(r.get("actual_window") for r in ok):
        out.append("**Time-window fidelity:** every observed call used exactly the "
                   "window requested.\n")

    # --- count consistency --------------------------------------------------
    inconsistent = [r for r in ok if r.get("bucket_sum_vs_total") not in (None, 1.0)]
    if inconsistent:
        out.append("**Count consistency — buckets vs the tool's own stated total**\n")
        out.append("| call | total | bucket sum | ratio |")
        out.append("|---|---|---|---|")
        for r in inconsistent[:10]:
            out.append(f"| {r['label']} | {r.get('total', 0):,} | "
                       f"{r.get('bucket_sum', 0):,} | **{r['bucket_sum_vs_total']}×** |")
        out.append("")

    # --- volume ceiling -----------------------------------------------------
    vc = volume_ceiling(records, tool)
    if vc and (vc["largest_ok_min"] or vc["smallest_rejected_min"]):
        out.append("**Volume ceiling** (windows ending at the same instant)\n")
        if vc["largest_ok_min"]:
            out.append(f"- largest window accepted: **{vc['largest_ok_min']} min**")
        if vc["smallest_rejected_min"]:
            out.append(f"- smallest window rejected for volume: "
                       f"**{vc['smallest_rejected_min']} min**")
        if vc["counts_seen"]:
            out.append(f"- line counts it reported it would have processed: "
                       + ", ".join(vc["counts_seen"]))
        if not vc["smallest_rejected_min"]:
            out.append("- no volume rejection seen — not subject to the 1M cap "
                       "at any window tested")
        out.append("")

    # --- parameter sweep ----------------------------------------------------
    sweep = [r for r in mine if r["group"] == "matrix"]
    if sweep:
        out.append("**Parameter sweep**\n")
        out.append("| variation | result |")
        out.append("|---|---|")
        for r in sweep:
            if r["outcome"] == "ok":
                cell = []
                if r.get("total") is not None:
                    cell.append(f"total {r['total']:,}")
                if r.get("bucket_count") is not None:
                    cell.append(f"{r['bucket_count']} buckets")
                if r.get("stats"):
                    s = r["stats"]
                    cell.append(f"{s.get('total_unique_templates','?')} templates "
                                f"from {s.get('total_logs_fetched','?')} logs")
                cell.append(f"{r.get('response_chars',0):,} chars")
                if r.get("silent_empty"):
                    cell.append("**empty**")
                res = ", ".join(cell)
            else:
                res = f"**{r['error_label']}** — {one_line(r.get('error'), 90)}"
            out.append(f"| `{r['label']}` | {res} |")
        out.append("")

    # --- errors -------------------------------------------------------------
    if errs:
        out.append("**Errors produced**\n")
        out.append("| label | triggered by | message |")
        out.append("|---|---|---|")
        seen = set()
        for r in errs:
            key = (r["error_label"], r["label"])
            if key in seen:
                continue
            seen.add(key)
            out.append(f"| `{r['error_label']}` | {r['label']} | "
                       f"{one_line(r.get('error'), 150)} |")
        out.append("")

    if silent:
        out.append("**Succeeded but returned nothing** — indistinguishable from a "
                   "genuine empty result:\n")
        for r in silent:
            out.append(f"- `{r['label']}` → {fmt_args(r['arguments_sent'])[:160]}")
        out.append("")
    return out


def render(runs, records, schemas):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    doc = [
        "# Mezmo MCP log tools — behaviour reference",
        "",
        f"Generated {now} by `make_report.py` from "
        f"{len(runs)} probe run(s), {len(records)} tool calls.",
        "",
        "Everything below is **observed against a live account**, not taken from "
        "documentation. Mezmo publishes no error catalogue, so the error labels "
        "are this repo's own taxonomy (see `probe_tools.py: ERROR_SIGNATURES`).",
        "",
        "## Runs included",
        "",
        "| run | query | window | groups | calls |",
        "|---|---|---|---|---|",
    ]
    for path, run in runs:
        n = len(run.get("records", []))
        w = run.get("window", ["?", "?"])
        doc.append(f"| `{Path(path).name}` | `{run.get('query')}` | "
                   f"{w[0]} → {w[1]} | {', '.join(run.get('groups', []))} | {n} |")
    doc.append("")

    # ---- cross-cutting summary --------------------------------------------
    errs = [r for r in records if r["outcome"] == "error"]
    by_label = defaultdict(list)
    for r in errs:
        by_label[r["error_label"]].append(r)

    doc += ["## Error taxonomy (all tools)", "",
            "| label | count | tools | what it means |", "|---|---|---|---|"]
    for label, rows in sorted(by_label.items(), key=lambda kv: -len(kv[1])):
        tools = sorted({r["tool"].replace("_relative_time", "_rel")
                        .replace("_time_range", "_abs") for r in rows})
        doc.append(f"| `{label}` | {len(rows)} | {', '.join(tools)} | "
                   f"{one_line(rows[0].get('note'), 200)} |")
    doc.append("")

    shapes = {r.get("shape") for r in errs}
    if shapes == {"application"}:
        doc += ["> **Every** error observed arrived as a normal result carrying "
                "`is_error=true`, not as a raised exception. A caller that only "
                "wraps calls in `try/except` will treat all of them as success.",
                ""]

    silent = [r for r in records if r.get("silent_empty")]
    if silent:
        doc += ["## Silent empties", "",
                "Calls that returned success with no data. Nothing in the response "
                "distinguishes these from a genuine 'no logs matched':", "",
                "| tool | call | arguments |", "|---|---|---|"]
        for r in silent:
            doc.append(f"| `{r['tool']}` | {r['label']} | "
                       f"`{fmt_args(r['arguments_sent'])[:120]}` |")
        doc.append("")

    doc += ["## Tools", ""]
    for tool in TOOL_ORDER:
        doc += render_tool(tool, schemas.get(tool), records)

    other = sorted({r["tool"] for r in records} - set(TOOL_ORDER))
    for tool in other:
        doc += render_tool(tool, schemas.get(tool), records)

    return "\n".join(doc) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", default="out",
                    help="directory of tool_behavior_*.json, or one such file")
    ap.add_argument("--out", default="TOOL_REPORT.md")
    args = ap.parse_args()

    src = Path(args.inp)
    paths = sorted(src.glob("tool_behavior_*.json")) if src.is_dir() else [src]
    if not paths:
        raise SystemExit(f"No tool_behavior_*.json under {src}. Run probe_tools.py first.")

    runs = load(paths)
    records, schemas = [], {}
    for _, run in runs:
        records += run.get("records", [])
        schemas.update(run.get("schemas") or {})

    Path(args.out).write_text(render(runs, records, schemas), encoding="utf-8")
    print(f"{len(runs)} run(s), {len(records)} calls -> {args.out}")
    if not schemas:
        print("  NOTE: no schemas captured. Re-run probe_tools.py (older runs "
              "predate schema capture) for the parameter sections.")


if __name__ == "__main__":
    main()
