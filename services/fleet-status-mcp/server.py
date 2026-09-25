"""
Fleet Status MCP Server

Answers "is this robot actually on right now, and where is it?" -- context
Mezmo cannot provide, because a robot that is powered off simply has no
logs, and an empty log result looks identical to a broken query.

Data comes from the ROC dashboard's existing HTTP API
(robot-operations-center, default http://roc-ubu:3001), NOT from
re-implementing its Tailscale + SSH polling here. That dashboard already
runs `tailscale status` for presence and SSHes each online robot for live
metrics, on a 5-second loop, from a host that is on the Tailnet. Consuming
its result means:

  - no robot SSH key inside this container, and no second system SSHing
    every robot in the fleet on a loop;
  - no Tailscale daemon in the container (though Docker Desktop does route
    MagicDNS -- `roc-ubu` resolves and answers from in here today);
  - Aura and the humans watching the dashboard can never disagree about
    who is online, because it is literally the same number.

The cost is a dependency: if the ROC dashboard is down, presence is
unavailable. That is reported explicitly rather than being reported as
"offline", because "I cannot tell" and "it is off" lead to very different
conclusions during triage.

IMPORTANT SCOPE LIMIT, stated again in the tool docstrings because it is
the easy mistake: this is CURRENT state only. It cannot answer "was this
robot running at 2pm last Thursday". For that, log volume is the presence
oracle -- if a host was logging, it was up -- so use the Mezmo histogram
for a past window.
"""

import ipaddress
import os
import socket
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse

ROC_URL = os.environ.get("ROC_DASHBOARD_URL", "http://roc-ubu:3001").rstrip("/")
PORT = int(os.environ.get("PORT", "8094"))
TIMEOUT = float(os.environ.get("ROC_TIMEOUT", "10"))

mcp = FastMCP("fleet-status")


def _loopback_hint() -> str:
    """Explain the one failure that looks like a dead dashboard but isn't.

    If ROC_DASHBOARD_URL's hostname resolves to a loopback address INSIDE
    this container, the name is almost certainly the host's own hostname.
    Ubuntu maps that to 127.0.1.1 in /etc/hosts, so `roc-ubu` means "this
    machine" -- and inside a container, "this machine" is the container,
    where nothing is listening. It resolves fine and connects to nothing,
    which reads as a refused connection rather than a DNS error.

    This only happens when the dashboard and this stack share one host. It
    cost a real debugging session, so the error explains itself instead.
    """
    host = urlparse(ROC_URL).hostname or ""
    try:
        addr = socket.gethostbyname(host)
    except OSError:
        return (f" The hostname '{host}' does not resolve inside this "
                "container at all, so this is a DNS problem rather than a "
                "dead dashboard.")
    if not ipaddress.ip_address(addr).is_loopback:
        return ""
    return (
        f" NOTE: '{host}' resolves to {addr} inside this container, which is "
        "the CONTAINER's own loopback -- not the dashboard's host. This "
        "happens when the ROC dashboard runs on the same machine as this "
        "stack: the hostname means 'me', and 'me' differs inside a "
        "container. Set ROC_DASHBOARD_URL to that host's routable address "
        "(its Tailscale or LAN IP, e.g. http://100.x.y.z:3001) rather than "
        "its hostname. The dashboard must also be listening on 0.0.0.0, not "
        "only on 127.0.0.1 -- check with `ss -ltnp | grep 3001`."
    )


def _get(path: str) -> dict:
    """One GET against the ROC dashboard, with failures surfaced as a tool
    error naming the dashboard -- never swallowed into an empty result."""
    url = f"{ROC_URL}{path}"
    try:
        r = httpx.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        raise ToolError(
            f"Could not reach the ROC dashboard at {url} ({type(e).__name__}: {e}). "
            "Robot presence is UNKNOWN right now -- do not report robots as "
            "offline on the strength of this failure."
            + _loopback_hint()
        ) from e


def _age(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        seen = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = datetime.now(timezone.utc) - seen
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} minutes ago"
    hours = mins // 60
    if hours < 48:
        return f"{hours} hours ago"
    return f"{hours // 24} days ago"


def _directory() -> tuple[dict, dict, dict, dict]:
    """(device->site, siteInfo, customer->sites, site->customer).

    The dashboard exposes customer->[sites]; site->customer is the useful
    direction here and has to be inverted. siteInfo carries `location` and
    `timezone` per site -- NOT a customer field, which is easy to assume
    and wrong. Timezone matters: log timestamps are UTC, and anyone asking
    about "the morning shift" means the site's local morning."""
    sites = _get("/api/sites")
    customer_to_sites = sites.get("customerToSites") or {}
    site_to_customer = {
        site: customer
        for customer, site_list in customer_to_sites.items()
        for site in (site_list or [])
    }
    return (sites.get("deviceToSite") or {},
            sites.get("siteInfo") or {},
            customer_to_sites,
            site_to_customer)


@mcp.tool()
def get_robot_status(robot: str | None = None) -> dict:
    """Whether robots are online RIGHT NOW, and what they are currently doing.

    Use this whenever a question depends on whether a robot is actually
    running -- and especially before concluding that a log search found
    nothing because nothing happened. A powered-off robot produces no logs,
    which is indistinguishable from a quiet robot or a broken query unless
    you check here.

    THIS IS CURRENT STATE ONLY. It cannot tell you whether a robot was
    running at some past time. For that, use log volume as the presence
    signal: run the Mezmo histogram for that host over the window in
    question -- if it was logging, it was up.

    Args:
        robot: a single robot id (e.g. "gen1-prod17"). Omit to get the
            whole fleet.

    Returns per robot: whether it is online, how long since it was last
    seen, its site and customer, and -- when online -- its current mode,
    release version, and packages succeeded/failed this container.
    """
    status = _get("/api/status")
    device_to_site, site_info, _, site_to_customer = _directory()

    online = set(status.get("onlineDevices") or [])
    metrics = status.get("metrics") or {}
    last_seen = status.get("lastSeenOnline") or {}

    names = [robot] if robot else sorted(set(device_to_site) | online)
    if robot and robot not in device_to_site and robot not in online:
        raise ToolError(
            f"{robot!r} is not a robot the ROC dashboard knows about. "
            f"Known robots: {', '.join(sorted(device_to_site)) or '(none)'}"
        )

    robots = []
    for name in names:
        site = device_to_site.get(name)
        info = site_info.get(site) or {}
        entry = {
            "robot": name,
            "online": name in online,
            "site": site,
            "customer": site_to_customer.get(site),
            "location": info.get("location"),
            "timezone": info.get("timezone"),
            "last_seen": last_seen.get(name),
            "last_seen_ago": _age(last_seen.get(name)),
        }
        m = metrics.get(name)
        if m:
            entry["current"] = {
                "mode": m.get("state"),
                "release_version": m.get("version"),
                "packages_succeeded": m.get("picked"),
                "packages_failed": m.get("failed"),
            }
        robots.append(entry)

    up = [r["robot"] for r in robots if r["online"]]
    return {
        "as_of": status.get("lastUpdate"),
        "source": f"ROC dashboard {ROC_URL}",
        "online_count": len(up),
        "online": up,
        "offline": [r["robot"] for r in robots if not r["online"]],
        "robots": robots,
        "note": (
            "Current state only. 'offline' here means not on the Tailnet as "
            "of `as_of`; it does not mean the robot was offline earlier. For "
            "a past window, check whether the host was producing logs."
        ),
    }


@mcp.tool()
def get_fleet_directory() -> dict:
    """Which robots exist, and which customer and site each one belongs to.

    Use this to turn a robot id into somewhere a person recognises
    ("gen1-prod17" -> Cintas, Grayson), to find every robot at a named
    customer or site, or to check whether a robot id in a question is even
    real before searching logs for it. Changes rarely; safe to call once
    and reuse within a conversation.
    """
    device_to_site, site_info, customer_to_sites, site_to_customer = _directory()
    by_site: dict[str, list[str]] = {}
    for device, site in device_to_site.items():
        by_site.setdefault(site, []).append(device)
    return {
        "robot_count": len(device_to_site),
        "robots": {
            device: {
                "site": site,
                "customer": site_to_customer.get(site),
                "location": (site_info.get(site) or {}).get("location"),
                "timezone": (site_info.get(site) or {}).get("timezone"),
            }
            for device, site in sorted(device_to_site.items())
        },
        "robots_by_site": {s: sorted(v) for s, v in sorted(by_site.items())},
        "customer_to_sites": customer_to_sites,
        "note": (
            "`timezone` is the site's local zone. Log timestamps are UTC, so "
            "convert before describing a time of day to someone at that site."
        ),
    }


def main() -> int:
    print(f"Fleet status MCP -- upstream ROC dashboard: {ROC_URL}", flush=True)
    try:
        status = _get("/api/status")
        print(f"  reachable; {len(status.get('onlineDevices') or [])} robot(s) "
              f"online as of {status.get('lastUpdate')}", flush=True)
    except ToolError as e:
        # Not fatal: the dashboard may simply be down right now, and this
        # server is still useful the moment it comes back. Tool calls will
        # report the failure explicitly in the meantime.
        print(f"  WARNING: {e}", file=sys.stderr, flush=True)

    mcp.run(transport="streamable-http", host="0.0.0.0", port=PORT)
    return 0


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
    sys.exit(main())
