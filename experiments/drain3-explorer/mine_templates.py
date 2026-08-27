#!/usr/bin/env python3
"""
mine_templates.py — feed a batch of logs through Drain3 and report the
templates (clusters) it finds, sorted by how often each one occurs.

This is a standalone experiment, separate from the aura-mezmo web app /
Aura agent -- nothing here is wired into docker-compose. Point it at a
file, a directory, or a glob of log files and it prints (and optionally
saves) the templates Drain3 mined, so you can see what the output looks
like before deciding whether/how to integrate it further (e.g. as a new
logs-mcp tool, or to auto-populate the robot-shift-notes keyword table).

Two input shapes are understood, per line:
  - Mezmo/LogDNA JSON export (one JSON object per line, like the .jsonl
    files this project's logs-mcp already works with) -- pulls the
    free-text field named by --field (default: "message"), and also
    grabs "app"/"level"/a timestamp if present, purely for the summary
    breakdown. Drain3 itself is content-only and tracks no timestamps at
    all (see the LogCluster class -- just tokens, an id, and a count), so
    this script tracks first/last-seen per cluster itself, keyed off
    whichever timestamp field the line actually has: "timestamp_iso",
    then "timestamp" (epoch ms), then Mezmo's own ingestion clock "_ts"
    (present on every line regardless of source app -- the fallback that
    guarantees near-universal coverage).
  - Plain text logs -- the whole line is fed to Drain3 as-is (no
    timestamp available unless your own log format embeds one and you
    adapt extract_timestamp() to parse it).

Usage:
    python3 mine_templates.py --input path/to/logs.jsonl
    python3 mine_templates.py --input path/to/logs.jsonl --top 40 --output results.json
    python3 mine_templates.py --input dir_of_logs/ --input another.log
    python3 mine_templates.py --input logs.jsonl --limit 5000   # quick test run
    python3 mine_templates.py --input logs.jsonl --persist state.bin  # accumulate across runs

    # the actual "reduce what I feed the LLM" use case: collapse repeats
    # into one line per template, with a time range, sorted chronologically
    python3 mine_templates.py --input logs.jsonl --collapsed-output collapsed.txt

    # train once on a representative batch, save the catalog...
    python3 mine_templates.py --input historical.jsonl --persist catalog.bin
    # ...then classify new logs against ONLY that catalog: no new clusters
    # get created and no existing template gets mutated (see Drain3's
    # training-vs-inference docs). Anything that doesn't match is reported
    # as "unrecognized" instead of silently becoming a new template --
    # itself a useful signal (a log shape nothing in training ever saw).
    python3 mine_templates.py --input new_logs.jsonl --persist catalog.bin --mode infer

    # cluster each app's lines in its own Drain3 tree instead of one shared
    # pool -- keeps a high-volume app (fastloop) from dominating a
    # low-volume app's (api-server) templates. Not combinable with
    # --mode infer / --persist yet.
    python3 mine_templates.py --input export.jsonl --by-app --collapsed-output collapsed.txt
"""
import argparse
import glob
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from drain3 import TemplateMiner
from drain3.file_persistence import FilePersistence
from drain3.template_miner_config import TemplateMinerConfig

HERE = Path(__file__).resolve().parent

# Sentinel cluster id for inference-mode lines that matched nothing in the
# trained catalog. Real Drain3 cluster ids start at 1, so this can't collide.
UNRECOGNIZED_ID = -1
UNRECOGNIZED_TEMPLATE = "(unrecognized -- no match in the trained catalog)"


def iter_lines(input_patterns):
    """Yield (file_path, line_no, raw_line) for every non-blank line across
    all --input arguments, which may be a file, a directory, or a glob."""
    for pattern in input_patterns:
        is_glob = any(ch in pattern for ch in "*?[]")
        matches = sorted(glob.glob(pattern)) if is_glob else [pattern]
        if not matches:
            print(f"warning: no files matched '{pattern}'", file=sys.stderr)
        for path_str in matches:
            path = Path(path_str)
            if path.is_dir():
                files = sorted(p for p in path.rglob("*") if p.is_file())
            elif path.is_file():
                files = [path]
            else:
                print(f"warning: '{path}' is not a file or directory", file=sys.stderr)
                continue
            for f in files:
                with f.open("r", errors="replace") as fh:
                    for line_no, line in enumerate(fh, start=1):
                        line = line.rstrip("\n")
                        if line.strip():
                            yield f, line_no, line


def _epoch_seconds(value):
    """Best-effort conversion of a Mezmo timestamp value -- epoch
    milliseconds, epoch seconds, or an ISO 8601 string -- to epoch seconds
    (float), or None if it doesn't parse."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e12 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def extract_timestamp(obj, inner=None):
    """Best-effort timestamp for one line, preferring a human-readable
    field and falling back to Mezmo's own ingestion clock.

    Order: "timestamp_iso" -> "timestamp" (epoch ms) -> "_ts" (epoch ms,
    Mezmo's ingestion time -- present on essentially every line regardless
    of source app, even ones with no self-reported timestamp of their own).
    """
    for source in (obj, inner):
        if not source:
            continue
        for key in ("timestamp_iso", "timestamp"):
            if source.get(key) is not None:
                ts = _epoch_seconds(source[key])
                if ts is not None:
                    return ts
    if obj and obj.get("_ts") is not None:
        return _epoch_seconds(obj["_ts"])
    return None


def iso(epoch_seconds):
    if epoch_seconds is None:
        return None
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_text(raw_line, field, line_field="_line"):
    """Return (free_text, metadata) for one line.

    Mezmo full-account exports mix multiple shapes: app-level events (like
    taskloop/api-server) carry a clean top-level `message`, but plenty of
    lines only from other sources (audit, kernel, pickle_rosbridge, ...)
    have no top-level `message` at all -- only a raw `_line` string, which
    is itself sometimes JSON-encoded (nested `message`) and sometimes just
    the plain free-text line. Falling back through all three keeps those
    from collapsing into one giant "whole JSON blob" pseudo-template.

    A timestamp is attached to `meta` whenever one can be derived (see
    extract_timestamp), independently of which of those three text-shapes
    was used -- Drain3 never sees it, but the caller keeps it for tracking
    first/last-seen per cluster.
    """
    stripped = raw_line.strip()
    if not stripped.startswith("{"):
        return raw_line, {}

    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return raw_line, {}
    if not isinstance(obj, dict):
        return raw_line, {}

    inner = None
    raw_inner = obj.get(line_field)
    if isinstance(raw_inner, str) and raw_inner.lstrip().startswith("{"):
        try:
            parsed_inner = json.loads(raw_inner)
            if isinstance(parsed_inner, dict):
                inner = parsed_inner
        except json.JSONDecodeError:
            pass

    meta = {
        "app": obj.get("app") or obj.get("_app"),
        "level": obj.get("level") or (inner.get("level") if inner else None),
        "timestamp": extract_timestamp(obj, inner),
    }

    if field in obj and obj[field] is not None:
        return str(obj[field]), meta
    if inner is not None and field in inner and inner[field] is not None:
        return str(inner[field]), meta
    if raw_inner is not None:
        return str(raw_inner), meta
    return raw_line, meta


def build_template_miner(config_path, persist_path):
    config = TemplateMinerConfig()
    config.load(str(config_path))
    persistence = FilePersistence(str(persist_path)) if persist_path else None
    return TemplateMiner(persistence_handler=persistence, config=config)


def new_clusters_meta():
    return {
        "count": 0, "examples": [], "apps": set(), "levels": set(),
        "first_seen": None, "last_seen": None, "all_timestamps": [],
    }


def cluster_group(text_meta_pairs, mode, examples_cap, ts_cap, config_path, persist_path):
    """Run one independent Drain3 pass (its own tree, its own catalog) over
    one group of (text, meta) pairs -- either "everything" or one app's
    lines, depending on how the caller partitioned things. Returns
    (rows, clusters_meta, total, timestamped, unrecognized), where rows is
    a list of (cluster_id, template, count) sorted by count descending.
    """
    miner = build_template_miner(config_path, persist_path)
    clusters_meta = defaultdict(new_clusters_meta)
    total = timestamped = unrecognized = 0

    for text, meta in text_meta_pairs:
        total += 1
        if mode == "train":
            result = miner.add_log_message(text)
            cid = result["cluster_id"]
        else:
            matched = miner.match(text)
            if matched is None:
                cid = UNRECOGNIZED_ID
                unrecognized += 1
            else:
                cid = matched.cluster_id

        cm = clusters_meta[cid]
        cm["count"] += 1
        if len(cm["examples"]) < examples_cap:
            cm["examples"].append(text)
        if meta.get("app"):
            cm["apps"].add(meta["app"])
        if meta.get("level"):
            cm["levels"].add(meta["level"])
        ts = meta.get("timestamp")
        if ts is not None:
            timestamped += 1
            if cm["first_seen"] is None or ts < cm["first_seen"]:
                cm["first_seen"] = ts
            if cm["last_seen"] is None or ts > cm["last_seen"]:
                cm["last_seen"] = ts
            if len(cm["all_timestamps"]) <= ts_cap:
                cm["all_timestamps"].append(ts)

    if mode == "train":
        rows = sorted(
            ((c.cluster_id, c.get_template(), c.size) for c in miner.drain.clusters),
            key=lambda r: r[2], reverse=True,
        )
    else:
        template_lookup = {c.cluster_id: c.get_template() for c in miner.drain.clusters}
        template_lookup[UNRECOGNIZED_ID] = UNRECOGNIZED_TEMPLATE
        rows = sorted(
            ((cid, template_lookup.get(cid, f"(unknown cluster {cid})"), m["count"])
             for cid, m in clusters_meta.items()),
            key=lambda r: r[2], reverse=True,
        )
    return rows, clusters_meta, total, timestamped, unrecognized


def render_report(label, rows, clusters_meta, total, timestamped, unrecognized, mode, top, cap, indent=""):
    """Print one group's table to stdout. `label` is a header line (the
    app name in --by-app mode, or a generic description otherwise)."""
    print(f"\n{indent}== {label} ==")
    if mode == "train":
        print(f"{indent}{total} lines -> {len(rows)} distinct templates "
              f"({timestamped}/{total} had a usable timestamp)")
    else:
        pct = unrecognized / max(total, 1)
        print(f"{indent}{total} lines matched against a trained catalog; "
              f"{unrecognized} ({pct:.1%}) matched nothing")
    header = f"{indent}{'COUNT':>7}  {'ID':>4}  {'APPS':<20}  TEMPLATE"
    print(header)
    print(indent + "-" * (len(header) - len(indent)))
    for cid, template, count in rows[:top]:
        meta = clusters_meta.get(cid, {"apps": set()})
        apps = ",".join(sorted(meta["apps"])) or "-"
        print(f"{indent}{count:>7}  {cid:>4}  {apps:<20}  {template}")
    if len(rows) > top:
        print(f"{indent}... and {len(rows) - top} more (raise --top to see them)")


def build_output_rows(rows, clusters_meta, cap):
    """(cluster_id, template, count) rows -> the dicts --output writes."""
    out = []
    for cid, template, count in rows:
        meta = clusters_meta.get(cid, {"examples": [], "apps": set(), "levels": set()})
        has_full = count <= cap
        out.append(
            {
                "cluster_id": cid,
                "count": count,
                "template": template,
                "unrecognized": cid == UNRECOGNIZED_ID,
                "apps": sorted(meta["apps"]),
                "levels": sorted(meta["levels"]),
                "first_seen": iso(meta.get("first_seen")),
                "last_seen": iso(meta.get("last_seen")),
                "all_timestamps": (
                    [iso(t) for t in sorted(meta.get("all_timestamps", []))] if has_full else None
                ),
                "examples": meta["examples"],
            }
        )
    return out


def build_collapsed_lines(rows, clusters_meta, cap):
    """(cluster_id, template, count) rows -> the bracketed text lines
    --collapsed-output writes, chronologically sorted."""
    ordered = sorted(rows, key=lambda r: clusters_meta.get(r[0], {}).get("first_seen") or float("inf"))
    lines = []
    for cid, template, count in ordered:
        meta = clusters_meta.get(cid, {})
        if count <= cap and meta.get("all_timestamps"):
            when = ", ".join(iso(t) for t in sorted(meta["all_timestamps"]))
        else:
            first, last = iso(meta.get("first_seen")), iso(meta.get("last_seen"))
            when = f"{first} -> {last}" if (first and last and first != last) else (first or "no timestamp")
        apps = ",".join(sorted(meta.get("apps", set()))) or "-"
        levels = ",".join(sorted(meta.get("levels", set()))) or "-"
        flag = "  [SINGLE OCCURRENCE]" if count == 1 else ""
        lines.append(f"[{when}] x{count} ({apps}) [{levels}]{flag}  {template}")
    return lines


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--input", action="append", required=True,
        help="Log file, directory, or glob pattern. Repeatable.",
    )
    parser.add_argument(
        "--field", default="message",
        help="JSON field holding free text, for JSON-line inputs (default: message)",
    )
    parser.add_argument(
        "--line-field", default="_line",
        help="Fallback JSON field to use when --field is absent, e.g. Mezmo's raw "
             "'_line' envelope (default: _line). Set to '' to disable the fallback.",
    )
    parser.add_argument("--config", default=str(HERE / "drain3.ini"), help="Path to drain3.ini")
    parser.add_argument(
        "--persist", default=None,
        help="Optional file to save/load learned clusters across runs (accumulate knowledge over time)",
    )
    parser.add_argument(
        "--mode", choices=["train", "infer"], default="train",
        help="train (default): add_log_message() -- may create new clusters or generalize "
             "existing templates. infer: match() against an existing --persist catalog only "
             "-- never creates or modifies clusters; anything that doesn't match is reported "
             "as unrecognized rather than silently learned.",
    )
    parser.add_argument("--output", default=None, help="Optional path to write full results as JSON")
    parser.add_argument(
        "--collapsed-output", default=None,
        help="Optional path to write a chronologically-sorted, one-line-per-template "
             "transcript (count + time range + apps) -- the actual context-reduction "
             "output meant to be fed to an LLM instead of the raw lines.",
    )
    parser.add_argument("--top", type=int, default=25, help="How many clusters to print (default 25)")
    parser.add_argument("--examples", type=int, default=2, help="Example raw lines to keep per cluster")
    parser.add_argument("--limit", type=int, default=None, help="Stop after N lines (quick test runs)")
    parser.add_argument(
        "--full-timestamps-below", type=int, default=10,
        help="For clusters with this many occurrences or fewer, keep every occurrence's "
             "timestamp instead of collapsing to just first/last (default 10). Above this "
             "count, only first_seen/last_seen are kept -- a middle occurrence 3 of 3 is "
             "worth seeing exactly; occurrence 1,847 of 3,934 is not.",
    )
    parser.add_argument(
        "--by-app", action="store_true",
        help="Cluster each app's lines separately instead of pooling everything into one "
             "Drain3 tree. Prevents a high-volume app (e.g. fastloop) from dominating "
             "sim_th-driven merging decisions for a low-volume app's lines (e.g. api-server), "
             "and reports/writes results as one section per app, largest first. v1 limit: "
             "not combinable with --mode infer or --persist (per-app catalogs aren't "
             "supported yet).",
    )
    args = parser.parse_args()

    if args.mode == "infer" and not (args.persist and Path(args.persist).exists()):
        parser.error(
            "--mode infer requires --persist pointing at an existing trained catalog "
            "(run --mode train with --persist first)."
        )
    if args.by_app and (args.mode == "infer" or args.persist):
        parser.error(
            "--by-app can't be combined with --mode infer or --persist yet -- each app "
            "would need its own saved catalog, which isn't supported in this version."
        )

    cap = args.full_timestamps_below
    files_seen = set()
    total = 0
    items = []  # [(text, meta), ...] materialized once, grouped below

    for f, _line_no, raw_line in iter_lines(args.input):
        if args.limit and total >= args.limit:
            break
        files_seen.add(str(f))
        text, meta = extract_text(raw_line, args.field, args.line_field)
        items.append((text, meta))
        total += 1

    start = time.time()
    if args.by_app:
        groups = defaultdict(list)
        for text, meta in items:
            groups[meta.get("app") or "(unknown app)"].append((text, meta))
        group_order = sorted(groups, key=lambda app: len(groups[app]), reverse=True)
    else:
        groups = {"all apps": items}
        group_order = ["all apps"]

    results = {}
    for label in group_order:
        results[label] = cluster_group(
            groups[label], args.mode, args.examples, cap, args.config, args.persist
        )
    elapsed = time.time() - start

    grand_total = sum(r[2] for r in results.values())
    grand_timestamped = sum(r[3] for r in results.values())
    grand_unrecognized = sum(r[4] for r in results.values())

    print(
        f"\nProcessed {grand_total} lines from {len(files_seen)} file(s) in {elapsed:.2f}s "
        f"[{args.mode} mode]{' [by-app: ' + str(len(group_order)) + ' groups]' if args.by_app else ''}"
    )
    if args.mode == "train":
        total_templates = sum(len(r[0]) for r in results.values())
        print(f"Found {total_templates} distinct templates "
              f"({grand_timestamped}/{grand_total} lines had a usable timestamp)")
    else:
        pct = grand_unrecognized / max(grand_total, 1)
        print(
            f"{grand_unrecognized}/{grand_total} lines ({pct:.1%}) matched none of the "
            f"trained catalog ({grand_timestamped}/{grand_total} lines had a usable timestamp)"
        )

    for label in group_order:
        rows, clusters_meta, grp_total, grp_timestamped, grp_unrecognized = results[label]
        render_report(
            label, rows, clusters_meta, grp_total, grp_timestamped, grp_unrecognized,
            args.mode, args.top, cap, indent=("  " if args.by_app else ""),
        )

    if args.output:
        if args.by_app:
            full_results = {
                label: build_output_rows(results[label][0], results[label][1], cap)
                for label in group_order
            }
        else:
            rows, clusters_meta, *_ = results["all apps"]
            full_results = build_output_rows(rows, clusters_meta, cap)
        Path(args.output).write_text(json.dumps(full_results, indent=2), encoding="utf-8")
        n_templates = (
            sum(len(v) for v in full_results.values()) if args.by_app else len(full_results)
        )
        print(f"\nWrote full results ({n_templates} templates) to {args.output}")

    if args.collapsed_output:
        if args.by_app:
            out_lines = []
            for label in group_order:
                rows, clusters_meta, *_ = results[label]
                out_lines.append(f"== {label} ==")
                out_lines.extend(build_collapsed_lines(rows, clusters_meta, cap))
                out_lines.append("")
        else:
            rows, clusters_meta, *_ = results["all apps"]
            out_lines = build_collapsed_lines(rows, clusters_meta, cap)
        Path(args.collapsed_output).write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        n_out = sum(1 for l in out_lines if l.startswith("["))
        print(
            f"\nWrote collapsed transcript to {args.collapsed_output}: "
            f"{grand_total} raw lines -> {n_out} lines "
            f"({grand_total / max(n_out, 1):.0f}x reduction)"
        )


if __name__ == "__main__":
    main()
