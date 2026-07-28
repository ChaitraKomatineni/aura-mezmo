"""
Logs MCP Server

Exposes log files that users have uploaded through the web control panel
(shared Docker volume) as searchable tools for the Aura agent. Read-only:
the web app owns writes, this server only lists/reads/searches.
"""

import os
from pathlib import Path

from fastmcp import FastMCP

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/data/uploads"))

mcp = FastMCP("logs-mcp")


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


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8091)
