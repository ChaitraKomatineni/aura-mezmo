"""
Aura Mezmo Playground — web control panel

Small FastAPI app serving the single-page UI plus three backend concerns:
  - /api/upload, /api/logs   — log file upload + listing (feeds logs-mcp)
  - /api/freshdesk/search    — direct Freshdesk ticket search for the UI
  - /api/chat                — pass-through streaming proxy to Aura's
                                OpenAI-compatible /v1/chat/completions

The Aura agent itself reaches uploaded logs, live Mezmo logs, and
Freshdesk tickets through its own MCP tool servers (see config/aura.toml);
this app's Freshdesk/logs endpoints are for human browsing in the UI.
"""

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Query, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

AURA_BASE_URL = os.environ.get("AURA_BASE_URL", "http://aura:8080")
AURA_MODEL = os.environ.get("AURA_MODEL", "aura-mezmo")
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/data/uploads"))
FRESHDESK_DOMAIN = os.environ.get("FRESHDESK_DOMAIN", "")
FRESHDESK_API_KEY = os.environ.get("FRESHDESK_API_KEY", "")
DOMO_EMBED_URL = os.environ.get("DOMO_EMBED_URL", "")

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def freshdesk_base_url() -> str:
    """Build the Freshdesk API base URL from FRESHDESK_DOMAIN.

    Accepts either a bare subdomain (e.g. "acme" -> acme.freshdesk.com) or
    a full custom portal domain (e.g. "support.acme.com" or
    "https://support.acme.com/") mapped via Freshdesk's custom domain
    feature -- those already resolve on their own and must NOT get
    ".freshdesk.com" appended.
    """
    domain = re.sub(r"^https?://", "", FRESHDESK_DOMAIN.strip()).rstrip("/")
    if "." in domain:
        return f"https://{domain}/api/v2"
    return f"https://{domain}.freshdesk.com/api/v2"

app = FastAPI(title="Aura Mezmo Playground")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/config")
def config():
    """Client-side config the UI prefills itself with (no secrets)."""
    return {"domo_embed_url": DOMO_EMBED_URL}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    dest = UPLOAD_DIR / file.filename
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
    return {"name": file.filename, "size_bytes": dest.stat().st_size}


@app.get("/api/logs")
def list_logs():
    files = [
        {"name": p.name, "size_bytes": p.stat().st_size}
        for p in sorted(UPLOAD_DIR.iterdir())
        if p.is_file()
    ]
    return {"files": files}


@app.get("/api/freshdesk/search")
def freshdesk_search(q: str = Query(...)):
    if not FRESHDESK_DOMAIN or not FRESHDESK_API_KEY:
        return JSONResponse(
            {
                "error": "Freshdesk is not configured yet. Set FRESHDESK_DOMAIN "
                "and FRESHDESK_API_KEY in .env, then restart."
            }
        )
    try:
        with httpx.Client(
            base_url=freshdesk_base_url(),
            auth=(FRESHDESK_API_KEY, "X"),
            timeout=15.0,
        ) as client:
            escaped = q.replace('"', '\\"')
            resp = client.get("/search/tickets", params={"query": f'"{escaped}"'})
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return JSONResponse({"error": f"Freshdesk API error: {e.response.status_code}"})
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"Could not reach Freshdesk: {e}"})

    tickets = data.get("results", data if isinstance(data, list) else [])
    return {"tickets": tickets}


@app.get("/api/freshdesk/recent")
def freshdesk_recent(days: int = Query(2, ge=1, le=30)):
    """All tickets created or updated in the last `days` days, newest first."""
    if not FRESHDESK_DOMAIN or not FRESHDESK_API_KEY:
        return JSONResponse(
            {
                "error": "Freshdesk is not configured yet. Set FRESHDESK_DOMAIN "
                "and FRESHDESK_API_KEY in .env, then restart."
            }
        )
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    tickets = []
    try:
        with httpx.Client(
            base_url=freshdesk_base_url(),
            auth=(FRESHDESK_API_KEY, "X"),
            timeout=15.0,
        ) as client:
            page = 1
            while True:
                resp = client.get(
                    "/tickets",
                    params={
                        "updated_since": since,
                        "order_by": "created_at",
                        "order_type": "desc",
                        "per_page": 100,
                        "page": page,
                    },
                )
                resp.raise_for_status()
                batch = resp.json()
                tickets.extend(batch)
                # Freshdesk caps list pagination at 300 results (page 1-3 at
                # 100/page); stop early if a page comes back short.
                if len(batch) < 100 or page >= 3:
                    break
                page += 1
    except httpx.HTTPStatusError as e:
        return JSONResponse({"error": f"Freshdesk API error: {e.response.status_code}"})
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"Could not reach Freshdesk: {e}"})

    return {"since": since, "count": len(tickets), "tickets": tickets}


@app.get("/api/freshdesk/ticket/{ticket_id}")
def freshdesk_ticket(ticket_id: int):
    """Full ticket detail (description, timestamps) used to prefill the
    correlation filters — search results alone don't include the body."""
    if not FRESHDESK_DOMAIN or not FRESHDESK_API_KEY:
        return JSONResponse(
            {
                "error": "Freshdesk is not configured yet. Set FRESHDESK_DOMAIN "
                "and FRESHDESK_API_KEY in .env, then restart."
            }
        )
    try:
        with httpx.Client(
            base_url=freshdesk_base_url(),
            auth=(FRESHDESK_API_KEY, "X"),
            timeout=15.0,
        ) as client:
            resp = client.get(f"/tickets/{ticket_id}")
            resp.raise_for_status()
            ticket = resp.json()
    except httpx.HTTPStatusError as e:
        return JSONResponse({"error": f"Freshdesk API error: {e.response.status_code}"})
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"Could not reach Freshdesk: {e}"})

    return {
        "id": ticket.get("id"),
        "subject": ticket.get("subject"),
        "description_text": ticket.get("description_text"),
        "status": ticket.get("status"),
        "priority": ticket.get("priority"),
        "tags": ticket.get("tags"),
        "created_at": ticket.get("created_at"),
        "updated_at": ticket.get("updated_at"),
    }


@app.post("/api/chat")
async def chat(payload: dict):
    messages = payload.get("messages", [])

    async def upstream():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{AURA_BASE_URL}/v1/chat/completions",
                json={"model": AURA_MODEL, "messages": messages, "stream": True},
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk

    return StreamingResponse(upstream(), media_type="text/event-stream")


# Static UI last, so the /api/* routes above take precedence.
app.mount("/", StaticFiles(directory="public", html=True), name="static")
