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
