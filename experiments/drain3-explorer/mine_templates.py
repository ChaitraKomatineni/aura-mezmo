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
    args = parser.parse_args()

    miner = build_template_miner(args.config, args.persist)

    # cluster_id -> {"examples": [...], "apps": {...}, "levels": {...},
    #                 "first_seen": epoch_seconds | None, "last_seen": ...}
    clusters_meta = defaultdict(
        lambda: {"examples": [], "apps": set(), "levels": set(), "first_seen": None, "last_seen": None}
    )
    files_seen = set()
    total = 0
    timestamped = 0
    start = time.time()

    for f, _line_no, raw_line in iter_lines(args.input):
        if args.limit and total >= args.limit:
            break
        files_seen.add(str(f))
        text, meta = extract_text(raw_line, args.field, args.line_field)
        result = miner.add_log_message(text)
        total += 1

        cm = clusters_meta[result["cluster_id"]]
        if len(cm["examples"]) < args.examples:
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

    elapsed = time.time() - start
    clusters = sorted(miner.drain.clusters, key=lambda c: c.size, reverse=True)

    print(f"\nProcessed {total} lines from {len(files_seen)} file(s) in {elapsed:.2f}s")
    print(f"Found {len(clusters)} distinct templates ({timestamped}/{total} lines had a usable timestamp)\n")

    header = f"{'COUNT':>7}  {'ID':>4}  {'APPS':<20}  TEMPLATE"
    print(header)
    print("-" * len(header))
    for cluster in clusters[: args.top]:
        meta = clusters_meta.get(cluster.cluster_id, {"apps": set(), "levels": set()})
        apps = ",".join(sorted(meta["apps"])) or "-"
        print(f"{cluster.size:>7}  {cluster.cluster_id:>4}  {apps:<20}  {cluster.get_template()}")

    if len(clusters) > args.top:
        print(f"\n... and {len(clusters) - args.top} more (raise --top to see them)")

    if args.output:
        full_results = []
        for cluster in clusters:
            meta = clusters_meta.get(cluster.cluster_id, {"examples": [], "apps": set(), "levels": set()})
            full_results.append(
                {
                    "cluster_id": cluster.cluster_id,
                    "count": cluster.size,
                    "template": cluster.get_template(),
                    "apps": sorted(meta["apps"]),
                    "levels": sorted(meta["levels"]),
                    "first_seen": iso(meta.get("first_seen")),
                    "last_seen": iso(meta.get("last_seen")),
                    "examples": meta["examples"],
                }
            )
        Path(args.output).write_text(json.dumps(full_results, indent=2), encoding="utf-8")
        print(f"\nWrote full results ({len(full_results)} templates) to {args.output}")

    if args.collapsed_output:
        # Chronological, not count-sorted -- this is meant to read like a
        # timeline an LLM can follow, not a leaderboard. Clusters with no
        # derivable timestamp sort last rather than crashing the sort.
        ordered = sorted(
            clusters,
            key=lambda c: clusters_meta.get(c.cluster_id, {}).get("first_seen") or float("inf"),
        )
        out_lines = []
        for cluster in ordered:
            meta = clusters_meta.get(cluster.cluster_id, {})
            first, last = iso(meta.get("first_seen")), iso(meta.get("last_seen"))
            if first and last and first != last:
                when = f"{first} -> {last}"
            else:
                when = first or "no timestamp"
            apps = ",".join(sorted(meta.get("apps", set()))) or "-"
            # Not every source tags severity -- audit/kernel/tailscaled lines
            # in a Mezmo export have no "level" field at all, so "-" here is
            # an honest "no severity reported", not a missing-data bug.
            levels = ",".join(sorted(meta.get("levels", set()))) or "-"
            flag = "  [SINGLE OCCURRENCE]" if cluster.size == 1 else ""
            out_lines.append(f"[{when}] x{cluster.size} ({apps}) [{levels}]{flag}  {cluster.get_template()}")
        Path(args.collapsed_output).write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        print(
            f"\nWrote collapsed transcript to {args.collapsed_output}: "
            f"{total} raw lines -> {len(out_lines)} lines "
            f"({total / max(len(out_lines), 1):.0f}x reduction)"
        )


if __name__ == "__main__":
    main()
