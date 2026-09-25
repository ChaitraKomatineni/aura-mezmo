"""
Logs MCP Server

Exposes log files that users have uploaded through the web control panel
(shared Docker volume) as searchable tools for the Aura agent. Read-only:
the web app owns writes, this server only lists/reads/searches.
"""

import os
from pathlib import Path

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from template_mining import build_output_rows, mine_file, read_occurrences

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/data/uploads"))
DRAIN3_CONFIG = Path(os.environ.get("DRAIN3_CONFIG", "/app/drain3.ini"))

mcp = FastMCP("logs-mcp")

# In-memory cache: (filename, by_app) -> (file_signature, mine_file() result).
# A plain module-level dict is enough here -- this server is a single
# long-lived process (docker-compose restarts it, doesn't recreate it per
# request) and mining a real 20,000-line file takes well under a second,
# so there's no case yet for anything heavier (a database, a background
# job). file_signature (mtime, size) invalidates the cache automatically
# if a file gets re-uploaded under the same name.
_mine_cache: dict[tuple[str, bool], tuple[tuple[float, int], dict]] = {}


def _file_signature(path: Path) -> tuple[float, int]:
    stat = path.stat()
    return (stat.st_mtime, stat.st_size)


def _mined(filename: str, by_app: bool) -> dict:
    """Return this file's mine_file() result, reusing the cache when the
    file hasn't changed since it was last mined in this same mode."""
    path = _safe_path(filename)
    if not path.is_file():
        raise ValueError(f"No such uploaded log: {filename!r}")
    sig = _file_signature(path)
    key = (filename, by_app)
    cached = _mine_cache.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]
    result = mine_file(path, DRAIN3_CONFIG, by_app=by_app)
    _mine_cache[key] = (sig, result)
    return result


def _safe_path(filename: str) -> Path:
    """Resolve a filename to a path inside UPLOAD_DIR, rejecting traversal."""
    candidate = (UPLOAD_DIR / filename).resolve()
    if not str(candidate).startswith(str(UPLOAD_DIR.resolve())):
        raise ValueError(f"Invalid filename: {filename!r}")
    return candidate


@mcp.tool()
def list_uploaded_logs() -> list[dict]:
    """List log files the user has uploaded via the web UI.

    Returns each file's name, size in bytes, and line count.
    """
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for path in sorted(UPLOAD_DIR.iterdir()):
        if not path.is_file():
            continue
        try:
            line_count = sum(1 for _ in path.open("r", errors="replace"))
        except OSError:
            line_count = None
        results.append(
            {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "line_count": line_count,
            }
        )
    return results


@mcp.tool()
def read_log(filename: str, offset: int = 0, limit: int = 200) -> dict:
    """Read a slice of lines from an uploaded log file.

    Args:
        filename: name of a file returned by list_uploaded_logs.
        offset: zero-based line number to start from.
        limit: maximum number of lines to return (default 200).
    """
    path = _safe_path(filename)
    if not path.is_file():
        return {"error": f"No such uploaded log: {filename!r}"}

    with path.open("r", errors="replace") as f:
        lines = f.readlines()

    total = len(lines)
    selected = lines[offset : offset + limit]
    return {
        "name": filename,
        "total_lines": total,
        "offset": offset,
        "returned_lines": len(selected),
        "lines": [line.rstrip("\n") for line in selected],
    }


@mcp.tool()
def search_logs(pattern: str, filename: str | None = None, max_matches: int = 100) -> dict:
    """Case-insensitive substring search across uploaded log files.

    Args:
        pattern: substring to search for (e.g. an error string, ticket id,
            timestamp fragment, or service name).
        filename: restrict the search to a single uploaded file; omit to
            search every uploaded file.
        max_matches: cap on the number of matching lines returned.
    """
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    needle = pattern.lower()
    targets = [_safe_path(filename)] if filename else sorted(UPLOAD_DIR.iterdir())

    matches = []
    for path in targets:
        if not path.is_file():
            continue
        with path.open("r", errors="replace") as f:
            for line_no, line in enumerate(f):
                if needle in line.lower():
                    matches.append(
                        {
                            "file": path.name,
                            "line_number": line_no,
                            "line": line.rstrip("\n"),
                        }
                    )
                    if len(matches) >= max_matches:
                        break
        if len(matches) >= max_matches:
            break

    return {"pattern": pattern, "match_count": len(matches), "matches": matches}


@mcp.tool()
def summarize_templates(
    filename: str,
    by_app: bool = False,
    min_count: int | None = None,
    level: str | None = None,
    top: int = 50,
) -> dict | list:
    """Cluster an uploaded log file into templates (Drain3) instead of
    returning raw lines -- a compact census of what happened and how
    often, with repetition already collapsed out. Call this BEFORE
    read_log/search_logs for any "what's in this file" / "what happened"
    question: it's far cheaper on context than paging through raw lines,
    and each returned row carries a cluster_id you can pass to
    lookup_template if you need every real occurrence of one specific row
    (this tool only keeps a couple of example lines and a capped
    timestamp list per template, on purpose, to stay compact).

    Args:
        filename: name of a file returned by list_uploaded_logs.
        by_app: cluster each app's lines in its own tree instead of
            pooling everything together. Prevents a high-volume app from
            dominating which templates a low-volume app's lines fall
            into. When true, the result is keyed by app name instead of
            being one flat list.
        min_count: only return templates that occurred at least this
            many times (filters out rare noise when you only care about
            routine/high-volume behavior).
        level: only return templates that carry this severity level
            (e.g. "ERROR", "WARN") on at least one occurrence.
        top: cap on templates returned per group, largest count first
            (default 50). Raise it if you need the long tail; the
            underlying mining always runs over every line regardless.
    """
    try:
        result = _mined(filename, by_app)
    except ValueError as e:
        return {"error": str(e)}
    out = {}
    for label in result["group_order"]:
        g = result["groups"][label]
        rows = build_output_rows(g["rows"], g["clusters_meta"], cap=10)
        if min_count is not None:
            rows = [r for r in rows if r["count"] >= min_count]
        if level is not None:
            rows = [r for r in rows if level.upper() in [lv.upper() for lv in r["levels"]]]
        out[label] = rows[:top]
    return out if by_app else out["all"]


@mcp.tool()
def lookup_template(
    filename: str,
    cluster_id: int,
    by_app: bool = False,
    group: str = "all",
    limit: int = 50,
) -> dict:
    """Return EVERY real occurrence of one template -- full raw line and
    real timestamp for each, not the couple of capped examples
    summarize_templates keeps. Use this once you've already found the
    cluster_id you care about from summarize_templates (same filename,
    same by_app value, and the group it came from if by_app was true).

    Args:
        filename: name of a file returned by list_uploaded_logs.
        cluster_id: the cluster_id from a prior summarize_templates call.
        by_app: must match the by_app value used in that prior call --
            cluster ids are only meaningful within the same clustering
            run that produced them.
        group: the app name the cluster_id came from, if by_app was
            true; leave as "all" for pooled (by_app=False) results.
        limit: cap on occurrences returned (a cluster can have
            thousands); omit/raise it if you actually need all of them.
    """
    try:
        result = _mined(filename, by_app)
    except ValueError as e:
        return {"error": str(e)}
    if group not in result["groups"]:
        return {
            "error": f"No group {group!r} in this file's mining result.",
            "available_groups": result["group_order"],
        }
    g = result["groups"][group]
    postings = g["postings"].get(cluster_id)
    if postings is None:
        return {"error": f"No cluster {cluster_id} in group {group!r} for {filename!r}."}

    selected = postings[:limit] if limit else postings
    occurrences = read_occurrences(selected)
    return {
        "cluster_id": cluster_id,
        "group": group,
        "total_occurrences": len(postings),
        "returned": len(occurrences),
        "occurrences": occurrences,
    }


# Liveness for the Docker healthcheck. Deliberately NOT an MCP call: a
# POST to /mcp with `initialize` mints a session, FastMCP keeps every one
# of them, and at a 10s interval that is 8,640 sessions a day against a
# 10,000 ceiling -- so the healthcheck itself took the server down after
# about 28 hours with "Refusing to open a new session". A plain GET
# creates no session.
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8091)
