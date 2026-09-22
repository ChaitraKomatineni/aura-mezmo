"""Replay a bug report's tool calls against the live stack.

A report records the exact arguments Aura sent. This re-sends them through
mezmo-proxy and prints what comes back NOW, next to what came back THEN.
That distinguishes the three things a "wrong answer" can actually be:

  - the query was wrong      -> replay returns the same little, and you can
                                see the bad window or the loose field match
                                sitting in the arguments
  - the filters hid it       -> replay returns the same little, but widening
                                the window or checking describe_scope shows
                                the data exists upstream
  - Aura misread good data   -> replay returns plenty; the tool was fine and
                                the answer was the problem

Usage, from the repo root:

    docker compose exec mezmo-proxy python - < scripts/replay_report.py

    # or a specific report, newest first by default:
    REPORT_ID=2303fb00bfed docker compose exec mezmo-proxy \
        python - < scripts/replay_report.py

Reads reports/reports.jsonl from the host via the path baked below; pass
REPORTS_FILE to point somewhere else. Runs inside the mezmo-proxy
container because that is where fastmcp and the proxy URL are reachable.
"""
import asyncio
import json
import os
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8093/mcp")
REPORTS_FILE = os.environ.get("REPORTS_FILE", "/reports/reports.jsonl")
REPORT_ID = os.environ.get("REPORT_ID")

# Only these are safe and meaningful to replay. freshdesk_*/fleet_* reflect
# state that has moved on since the report, and logs_* read uploaded files
# that may be gone -- replaying those proves nothing.
REPLAYABLE_PREFIXES = ("get_", "deduplicate_", "analyze_", "group_")


def load_reports(path):
    reports, statuses = {}, {}
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        sys.exit(f"No reports file at {path}. Mount it or set REPORTS_FILE.")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") == "status" and row.get("report_id"):
            statuses[row["report_id"]] = row.get("status")
        elif row.get("id"):
            reports[row["id"]] = row
    for rid, st in statuses.items():
        if rid in reports:
            reports[rid]["status"] = st
    return sorted(reports.values(), key=lambda r: r.get("created_at", ""),
                  reverse=True)


def strip_prefix(name):
    """Reports store Aura-side names (mezmo_get_log_histogram); the proxy
    serves them unprefixed."""
    return name.split("_", 1)[1] if name.startswith("mezmo_") else name


async def call(client, tool, args, tries=3):
    last = "?"
    for _ in range(tries):
        try:
            r = await client.call_tool(tool, args, raise_on_error=False)
        except Exception as exc:                                # noqa: BLE001
            last = f"RAISED {type(exc).__name__}"
            await asyncio.sleep(2)
            continue
        text = " ".join(b.text for b in (r.content or [])
                        if getattr(b, "text", None))
        if getattr(r, "is_error", False):
            return False, text.strip().splitlines()[0][:90]
        return True, text
    return False, last


async def main():
    reports = load_reports(REPORTS_FILE)
    if not reports:
        sys.exit("No reports recorded yet.")

    if REPORT_ID:
        report = next((r for r in reports if r["id"] == REPORT_ID), None)
        if report is None:
            sys.exit(f"No report with id {REPORT_ID}. "
                     f"Have: {', '.join(r['id'] for r in reports[:10])}")
    else:
        report = reports[0]
        print(f"(no REPORT_ID given -- replaying the newest of "
              f"{len(reports)} reports)\n")

    print("=" * 72)
    print(f"report   {report['id']}   {report.get('created_at', '')}")
    print(f"reporter {report.get('reporter') or 'anonymous'}"
          f"   category {report.get('category')}"
          f"   status {report.get('status', 'open')}")
    print(f"asked    {report.get('question', '')[:200]}")
    if report.get("note"):
        print(f"expected {report['note'][:200]}")
    answer = (report.get("answer") or "").replace("\n", " ")
    print(f"answered {answer[:200]}")
    print("=" * 72)

    calls = report.get("tool_calls") or []
    if not calls:
        print("\nNo tool calls recorded -- Aura answered without querying "
              "anything. That is itself the finding.")
        return

    async with Client(StreamableHttpTransport(url=PROXY_URL)) as client:
        for i, c in enumerate(calls, 1):
            name = strip_prefix(c.get("tool_name", ""))
            args = c.get("arguments") or {}
            print(f"\n--- {i}/{len(calls)}  {name}")
            for k in ("query", "from_time", "to_time", "granularity"):
                if k in args:
                    print(f"      {k:12} {args[k]}")
            extra = {k: v for k, v in args.items()
                     if k not in ("query", "from_time", "to_time", "granularity")}
            if extra:
                print(f"      {'other':12} {json.dumps(extra)}")

            then = ("FAILED: " + (c.get("error") or "?")
                    if c.get("success") is False
                    else f"{c.get('result_chars', 0):,} chars")
            print(f"      then         {then}")

            if not name.startswith(REPLAYABLE_PREFIXES):
                print("      now          (not replayed -- not a log query)")
                continue

            ok, out = await call(client, name, args)
            if not ok:
                print(f"      now          FAILED: {out}")
                continue
            total = None
            try:
                total = json.loads(out).get("total")
            except Exception:                                   # noqa: BLE001
                pass
            print(f"      now          {len(out):,} chars"
                  + (f", total={total:,}" if isinstance(total, int) else ""))

    print("\nIf 'now' matches 'then', the query is the problem -- read the "
          "window and the field matches above.\nIf 'now' is much larger, "
          "something transient or a filter change was involved.")


asyncio.run(main())
