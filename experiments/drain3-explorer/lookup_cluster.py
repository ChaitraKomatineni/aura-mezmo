#!/usr/bin/env python3
"""
lookup_cluster.py — the drill-down half of the toolkit. Given a
mine_templates.py --index-output file and a cluster id, seek straight back
into the original log file(s) and print EVERY real line that landed in
that cluster, with its real timestamp -- not the couple of capped
examples/timestamps --output keeps for high-volume clusters, all of them.

Why this needs its own file instead of a flag on view_results.py: the
index only stores pointers (file, byte offset, line number, timestamp),
not the actual line text -- so answering "show me everything for cluster
N" means opening the original log file(s) again and seeking to each
pointer, which view_results.py's JSON-only viewer has no reason to do.

Usage:
    python3 mine_templates.py --input logs.jsonl --output results.json --index-output results_index.json
    python3 lookup_cluster.py --index results_index.json --cluster 17
    python3 lookup_cluster.py --index results_index.json --cluster 17 --group fastloop   # --by-app runs only
    python3 lookup_cluster.py --index results_index.json --cluster 17 --limit 20
    python3 lookup_cluster.py --index results_index.json --list-groups
"""
import argparse
import json
from pathlib import Path


def load_index(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def resolve_postings(index_doc, cluster_id, group=None):
    """Pick out one cluster's pointer list, handling both index shapes
    (pooled: top-level "postings"; --by-app: nested under "groups")."""
    if "groups" in index_doc:
        if group is None:
            raise SystemExit(
                "This index was built with --by-app -- pass --group to pick which "
                "app's cluster tree to look in. Available groups:\n  "
                + "\n  ".join(index_doc["groups"])
            )
        if group not in index_doc["groups"]:
            raise SystemExit(f"No group '{group}' in this index. Available: {', '.join(index_doc['groups'])}")
        postings = index_doc["groups"][group]
    else:
        postings = index_doc.get("postings", {})
    return postings.get(str(cluster_id))


def read_occurrences(files, postings):
    """Read every pointer's raw line back out of its source file.

    Opens each referenced file once (not once per pointer -- with
    thousands of pointers into the same file, reopening per line would
    dominate the runtime for no reason) and seeks directly to each
    line's byte offset instead of scanning from the start.
    """
    handles = {}
    rows = []
    try:
        for file_idx, offset, line_no, timestamp in postings:
            path = files[file_idx]
            fh = handles.get(path)
            if fh is None:
                fh = open(path, "r", errors="replace")
                handles[path] = fh
            fh.seek(offset)
            raw = fh.readline().rstrip("\n")
            rows.append({"line_no": line_no, "timestamp": timestamp, "file": path, "text": raw})
    finally:
        for fh in handles.values():
            fh.close()
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--index", required=True, help="--index-output file from mine_templates.py")
    parser.add_argument("--cluster", type=int, help="Cluster id to look up (matches --output's cluster_id)")
    parser.add_argument("--group", default=None, help="App group name -- required if the index was built with --by-app")
    parser.add_argument("--limit", type=int, default=None, help="Only print the first N occurrences (default: all)")
    parser.add_argument("--list-groups", action="store_true", help="List the app groups in this index and exit")
    args = parser.parse_args()

    index_doc = load_index(args.index)

    if args.list_groups:
        groups = index_doc.get("groups")
        if not groups:
            print("This index is pooled (no --by-app groups).")
        else:
            for g in groups:
                print(g)
        return

    if args.cluster is None:
        parser.error("--cluster is required (or pass --list-groups)")

    postings = resolve_postings(index_doc, args.cluster, args.group)
    if postings is None:
        raise SystemExit(f"No cluster {args.cluster} in this index.")

    files = index_doc["files"]
    rows = read_occurrences(files, postings[: args.limit] if args.limit else postings)

    print(f"Cluster {args.cluster}: {len(postings)} occurrence(s) total"
          + (f", showing {len(rows)}" if args.limit and args.limit < len(postings) else "") + "\n")
    for row in rows:
        name = Path(row["file"]).name
        print(f"[{row['timestamp']}] {name}:{row['line_no']}  {row['text']}")


if __name__ == "__main__":
    main()
