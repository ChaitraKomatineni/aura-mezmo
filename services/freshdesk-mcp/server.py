"""
Freshdesk MCP Server

Wraps the Freshdesk API v2 (https://developers.freshdesk.com/api/) so the
Aura agent can search and read support tickets/bugs, and correlate them
with log data from the other MCP servers.

Requires FRESHDESK_DOMAIN (either a bare subdomain, e.g. "acme" for
acme.freshdesk.com, or a full custom portal domain such as
"support.acme.com" if you've mapped one via Freshdesk's custom domain
feature) and FRESHDESK_API_KEY.
"""

import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from fastmcp import FastMCP

FRESHDESK_DOMAIN = os.environ.get("FRESHDESK_DOMAIN", "")
FRESHDESK_API_KEY = os.environ.get("FRESHDESK_API_KEY", "")

mcp = FastMCP("freshdesk-mcp")


def _base_url() -> str:
    domain = re.sub(r"^https?://", "", FRESHDESK_DOMAIN.strip()).rstrip("/")
    if "." in domain:
        return f"https://{domain}/api/v2"
    return f"https://{domain}.freshdesk.com/api/v2"


def _client() -> httpx.Client:
    if not FRESHDESK_DOMAIN or not FRESHDESK_API_KEY:
        raise RuntimeError(
            "Freshdesk is not configured: set FRESHDESK_DOMAIN and "
            "FRESHDESK_API_KEY in .env"
        )
    return httpx.Client(
        base_url=_base_url(),
        auth=(FRESHDESK_API_KEY, "X"),
        timeout=15.0,
    )


def _summarize(ticket: dict) -> dict:
    return {
        "id": ticket.get("id"),
        "subject": ticket.get("subject"),
        "status": ticket.get("status"),
        "priority": ticket.get("priority"),
        "type": ticket.get("type"),
        "tags": ticket.get("tags"),
        "created_at": ticket.get("created_at"),
        "updated_at": ticket.get("updated_at"),
    }


# Operators fill these in on the ticket form, and they are worth far more
# to an RCA than the free-text description: they are structured choices
# rather than someone's wording. cf_robot_name and the override window
# are the "which robot, which minutes" that log triage needs, and
# crucially the override window is when the FAILURE happened -- tickets
# are routinely filed hours later, so created_at is the wrong anchor.
_ET = "America/New_York"
_OVERRIDE_FORMATS = ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M")


def _parse_override(value):
    """'09/18/2026 10:20 PM ET' -> aware UTC datetime, or None.

    The trailing zone label is stripped and the time interpreted as
    US Eastern, which is what the form means by 'ET'; zoneinfo resolves
    EST vs EDT for the date itself rather than assuming a fixed offset.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for suffix in (" ET", " EST", " EDT"):
        if text.upper().endswith(suffix):
            text = text[: -len(suffix)].strip()
            break
    for fmt in _OVERRIDE_FORMATS:
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        try:
            return naive.replace(tzinfo=ZoneInfo(_ET)).astimezone(timezone.utc)
        except Exception:  # noqa: BLE001 -- missing tzdata shouldn't 500 the tool
            return None
    return None


def _rca_hints(ticket: dict) -> dict:
    """The few fields an RCA actually needs, resolved and in UTC, so the
    agent does not have to parse American dates or convert zones."""
    cf = ticket.get("custom_fields") or {}
    start = _parse_override(cf.get("cf_start_override"))
    end = _parse_override(cf.get("cf_end_override"))
    hints = {
        "robot": cf.get("cf_robot_name"),
        "subsystem": next((v for k, v in cf.items()
                           if k.startswith("cf_subsystem") and v), None),
        "release_version": cf.get("cf_release_version"),
        "failure_date": cf.get("cf_time_of_failure"),
        "window_start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ") if start else None,
        "window_end_utc": end.strftime("%Y-%m-%dT%H:%M:%SZ") if end else None,
        "window_source": None,
    }
    if start and end:
        hints["window_source"] = (
            "cf_start_override/cf_end_override (operator-stated failure window, "
            "converted from ET). Prefer this over created_at -- tickets are "
            "often filed well after the event."
        )
    elif ticket.get("created_at"):
        hints["window_source"] = (
            "No override window on this ticket. created_at is when it was "
            "FILED, not when the failure happened -- widen the search window "
            "accordingly and say that you did."
        )
    return {k: v for k, v in hints.items() if v not in (None, "", [])}


@mcp.tool()
def search_tickets(query: str, max_results: int = 20) -> dict:
    """Full-text search Freshdesk tickets by subject/description.

    Args:
        query: free-text search term (e.g. an error message, feature name,
            or customer name).
        max_results: cap on the number of tickets returned (default 20).
    """
    try:
        with _client() as client:
            # Freshdesk's search DSL wants a quoted field query; subject is
            # the most useful free-text field for bug/keyword search.
            escaped = query.replace('"', '\\"')
            resp = client.get(
                "/search/tickets", params={"query": f'"{escaped}"'}
            )
            resp.raise_for_status()
            data = resp.json()
    except RuntimeError as e:
        return {"error": str(e)}
    except httpx.HTTPStatusError as e:
        return {"error": f"Freshdesk API error: {e.response.status_code} {e.response.text}"}

    tickets = data.get("results", data if isinstance(data, list) else [])
    tickets = tickets[:max_results]
    return {"query": query, "count": len(tickets), "tickets": [_summarize(t) for t in tickets]}


@mcp.tool()
def get_ticket(ticket_id: int) -> dict:
    """Fetch full details for a single Freshdesk ticket, including description.

    Args:
        ticket_id: the Freshdesk ticket number.
    """
    try:
        with _client() as client:
            resp = client.get(f"/tickets/{ticket_id}")
            resp.raise_for_status()
            ticket = resp.json()
    except RuntimeError as e:
        return {"error": str(e)}
    except httpx.HTTPStatusError as e:
        return {"error": f"Freshdesk API error: {e.response.status_code} {e.response.text}"}

    summary = _summarize(ticket)
    summary["description_text"] = ticket.get("description_text")
    # Empty custom fields are dropped: a full Freshdesk form is ~23 keys
    # and most are blank on any given ticket, which is pure context cost.
    summary["custom_fields"] = {
        k: v for k, v in (ticket.get("custom_fields") or {}).items()
        if v not in (None, "", [])
    }
    summary["rca_hints"] = _rca_hints(ticket)
    return summary


@mcp.tool()
def list_recent_tickets(limit: int = 10) -> dict:
    """List the most recently updated Freshdesk tickets.

    Args:
        limit: number of tickets to return (default 10, max 100).
    """
    limit = max(1, min(limit, 100))
    try:
        with _client() as client:
            resp = client.get(
                "/tickets",
                params={"order_by": "updated_at", "order_type": "desc", "per_page": limit},
            )
            resp.raise_for_status()
            tickets = resp.json()
    except RuntimeError as e:
        return {"error": str(e)}
    except httpx.HTTPStatusError as e:
        return {"error": f"Freshdesk API error: {e.response.status_code} {e.response.text}"}

    return {"count": len(tickets), "tickets": [_summarize(t) for t in tickets]}


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8092)
