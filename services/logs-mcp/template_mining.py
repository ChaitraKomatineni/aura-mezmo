"""
template_mining.py — Drain3-based log template mining, importable by
server.py's summarize_templates/lookup_template tools.

Same design as experiments/drain3-explorer/mine_templates.py (same
postings-index drill-down approach, same masking/timestamp extraction
rules) but trimmed to just the reusable pieces a server needs: no
argparse/CLI, no glob/directory expansion, because the server always
mines exactly one already-resolved uploaded file per request rather than
a batch of --input patterns. See that file's module docstring and the
project README for the fuller design rationale.
"""
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig

# Sentinel cluster id for inference-mode lines that matched nothing in the
# trained catalog. Real Drain3 cluster ids start at 1, so this can't collide.
UNRECOGNIZED_ID = -1
UNRECOGNIZED_TEMPLATE = "(unrecognized -- no match in the trained catalog)"


def iter_file_lines(path: Path):
    """Yield (line_no, byte_offset, raw_line) for every non-blank line in
    one file. byte_offset is read via explicit readline()+tell(), not
    `for line in fh` -- Python's docs note tell() isn't reliable while
    iterating a text file with a for-loop, due to internal read-ahead
    buffering. This is what lets lookup_template seek straight back to
    one exact line later instead of re-scanning the file."""
    with path.open("r", errors="replace") as fh:
        line_no = 0
        while True:
            offset = fh.tell()
            raw = fh.readline()
            if not raw:
                break
            line_no += 1
            line = raw.rstrip("\n")
            if line.strip():
                yield line_no, offset, line


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
    """Best-effort timestamp for one line: "timestamp_iso" -> "timestamp"
    (epoch ms) -> "_ts" (Mezmo's own ingestion clock, present on
    essentially every line regardless of source app)."""
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


def extract_text(raw_line, field="message", line_field="_line"):
    """Return (free_text, metadata) for one line -- see mine_templates.py's
    extract_text for the full rationale on the message -> nested _line ->
    raw _line -> whole-line fallback chain."""
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


def build_template_miner(config_path):
    config = TemplateMinerConfig()
    config.load(str(config_path))
    return TemplateMiner(config=config)


def new_clusters_meta():
    return {
        "count": 0, "examples": [], "apps": set(), "levels": set(),
        "first_seen": None, "last_seen": None, "all_timestamps": [],
    }


def cluster_group(items, examples_cap, ts_cap, config_path):
    """Run one Drain3 pass (train mode only -- the server doesn't yet
    support --mode infer/--persist) over one group of (text, meta,
    pointer) triples. Builds clusters_meta (capped summary) and postings
    (uncapped inverted index: cluster_id -> [(file, offset, line_no,
    timestamp), ...]) side by side -- see mine_templates.py's
    cluster_group for the full rationale. Returns (rows, clusters_meta,
    postings) where rows is [(cluster_id, template, count), ...] sorted
    by count descending.
    """
    miner = build_template_miner(config_path)
    clusters_meta = defaultdict(new_clusters_meta)
    postings = defaultdict(list)

    for text, meta, pointer in items:
        result = miner.add_log_message(text)
        cid = result["cluster_id"]

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
            if cm["first_seen"] is None or ts < cm["first_seen"]:
                cm["first_seen"] = ts
            if cm["last_seen"] is None or ts > cm["last_seen"]:
                cm["last_seen"] = ts
            if len(cm["all_timestamps"]) <= ts_cap:
                cm["all_timestamps"].append(ts)

        postings[cid].append((pointer[0], pointer[1], pointer[2], ts))

    rows = sorted(
        ((c.cluster_id, c.get_template(), c.size) for c in miner.drain.clusters),
        key=lambda r: r[2], reverse=True,
    )
    return rows, clusters_meta, postings


def build_output_rows(rows, clusters_meta, cap):
    """(cluster_id, template, count) rows -> the dicts summarize_templates
    returns -- same shape mine_templates.py's --output writes."""
    out = []
    for cid, template, count in rows:
        meta = clusters_meta.get(cid, {"examples": [], "apps": set(), "levels": set()})
        has_full = count <= cap
        out.append(
            {
                "cluster_id": cid,
                "count": count,
                "template": template,
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


def read_occurrences(postings):
    """postings: list of (file_path_str, byte_offset, line_no,
    timestamp_epoch). Reads each line back by seeking directly to its
    offset, opening each referenced file once -- not once per pointer,
    which matters once a cluster has hundreds of occurrences in the same
    file. Returns full raw line text + real timestamp for every one."""
    handles = {}
    rows = []
    try:
        for file_path, offset, line_no, timestamp in postings:
            fh = handles.get(file_path)
            if fh is None:
                fh = open(file_path, "r", errors="replace")
                handles[file_path] = fh
            fh.seek(offset)
            raw = fh.readline().rstrip("\n")
            rows.append({"line_no": line_no, "timestamp": iso(timestamp), "text": raw})
    finally:
        for fh in handles.values():
            fh.close()
    return rows


def mine_file(path: Path, config_path: Path, field="message", line_field="_line",
              by_app=False, examples_cap=2, ts_cap=10, limit=None):
    """Mine one file end-to-end. Returns:
        {
          "by_app": bool,
          "group_order": [label, ...],       # largest group first
          "groups": {label: {"rows": [...], "clusters_meta": {...}, "postings": {...}}},
        }
    Pooled mode (by_app=False) always has exactly one group, labeled "all".
    """
    items = []
    for line_no, offset, raw_line in iter_file_lines(path):
        if limit and len(items) >= limit:
            break
        text, meta = extract_text(raw_line, field, line_field)
        items.append((text, meta, (str(path), offset, line_no)))

    if by_app:
        buckets = defaultdict(list)
        for item in items:
            buckets[item[1].get("app") or "(unknown app)"].append(item)
        group_order = sorted(buckets, key=lambda g: len(buckets[g]), reverse=True)
    else:
        buckets = {"all": items}
        group_order = ["all"]

    groups = {}
    for label in group_order:
        rows, clusters_meta, postings = cluster_group(buckets[label], examples_cap, ts_cap, config_path)
        groups[label] = {"rows": rows, "clusters_meta": clusters_meta, "postings": postings}

    return {"by_app": by_app, "group_order": group_order, "groups": groups}
