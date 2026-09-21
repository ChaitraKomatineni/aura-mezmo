"""
Aura Mezmo Playground — web control panel

Small FastAPI app serving the single-page UI plus four backend concerns:
  - /api/upload, /api/logs   — log file upload + listing (feeds logs-mcp)
  - /api/freshdesk/search    — direct Freshdesk ticket search for the UI
  - /api/skills              — CRUD for config/skills/*/SKILL.md, so people
                                without code access can author Agent Skills
                                through the "Skills" tab instead of editing
                                files directly. Aura only discovers skills at
                                startup, so a change here needs an operator
                                to run `docker compose restart aura` before
                                it's live in chat — the UI says so.
  - /api/chat                — pass-through streaming proxy to Aura's
                                OpenAI-compatible /v1/chat/completions

The Aura agent itself reaches uploaded logs, live Mezmo logs, and
Freshdesk tickets through its own MCP tool servers (see config/aura.toml);
this app's Freshdesk/logs endpoints are for human browsing in the UI.
"""

import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

AURA_BASE_URL = os.environ.get("AURA_BASE_URL", "http://aura:8080")
AURA_MODEL = os.environ.get("AURA_MODEL", "aura-mezmo")
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/data/uploads"))
FRESHDESK_DOMAIN = os.environ.get("FRESHDESK_DOMAIN", "")
FRESHDESK_API_KEY = os.environ.get("FRESHDESK_API_KEY", "")
SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", "/app/skills"))

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SKILLS_DIR.mkdir(parents=True, exist_ok=True)


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
    return {}


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


SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class SkillCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(..., min_length=1, max_length=1024)
    body: str = Field(..., min_length=1)
    # Optional by design, not enforced: this app has no login/user-identity
    # concept at all, so a required-but-unverified free-text field wouldn't
    # give real accountability, just the appearance of it. Existing skills
    # (e.g. robot-shift-notes) predate this field and simply show no author
    # until someone edits them.
    author: str | None = Field(None, max_length=128)


class SkillUpdate(BaseModel):
    description: str = Field(..., min_length=1, max_length=1024)
    body: str = Field(..., min_length=1)
    author: str | None = Field(None, max_length=128)


def validate_skill_name(name: str) -> None:
    """Mirror Aura's own SKILL.md name rules (agentskills.io spec) so a skill
    saved here is guaranteed to be one Aura will actually discover: 1-64
    chars, lowercase alphanumerics and hyphens, no leading/trailing/double
    hyphens."""
    if not name or len(name) > 64:
        raise HTTPException(400, f"Skill name must be 1-64 characters, got {len(name)}.")
    if not SKILL_NAME_RE.match(name):
        raise HTTPException(
            400,
            "Skill name can only use lowercase letters, digits, and single hyphens "
            "(no leading/trailing/double hyphens) — e.g. 'my-new-skill'.",
        )


def skill_dir(name: str) -> Path:
    """Resolve a skill's directory, refusing anything that would escape
    SKILLS_DIR even though validate_skill_name already blocks the
    characters needed to do that."""
    validate_skill_name(name)
    candidate = (SKILLS_DIR / name).resolve()
    if candidate.parent != SKILLS_DIR.resolve():
        raise HTTPException(400, "Invalid skill name.")
    return candidate


def read_skill_md(path: Path) -> dict:
    """Parse a SKILL.md's YAML frontmatter + body."""
    content = path.read_text(encoding="utf-8")
    if not content.lstrip().startswith("---"):
        raise HTTPException(500, f"{path} is missing YAML frontmatter.")
    after_first = content.lstrip()[3:]
    closing = after_first.find("---")
    if closing == -1:
        raise HTTPException(500, f"{path} is missing a closing '---'.")
    frontmatter = yaml.safe_load(after_first[:closing]) or {}
    body = after_first[closing + 3 :].lstrip("\n")
    return {
        "name": frontmatter.get("name", ""),
        "description": frontmatter.get("description", ""),
        # Absent on skills written before this field existed (e.g.
        # robot-shift-notes) -- None rather than "", so the UI can tell
        # "never set" apart from "set to empty" if that distinction ever
        # matters.
        "author": frontmatter.get("author"),
        "body": body,
    }


def write_skill_md(path: Path, name: str, description: str, body: str, author: str | None = None) -> None:
    frontmatter_dict = {"name": name, "description": description}
    if author:
        frontmatter_dict["author"] = author
    frontmatter = yaml.safe_dump(frontmatter_dict, sort_keys=False)
    path.write_text(f"---\n{frontmatter}---\n{body.rstrip()}\n", encoding="utf-8")


@app.get("/api/skills")
def list_skills():
    """All skills currently on disk, newest-edited first. Note: this reads
    straight from config/skills — it does not tell you whether Aura has
    picked up a given change yet (it only re-scans skills on startup)."""
    skills = []
    if SKILLS_DIR.exists():
        for entry in sorted(SKILLS_DIR.iterdir()):
            skill_file = entry / "SKILL.md"
            if entry.is_dir() and skill_file.exists():
                parsed = read_skill_md(skill_file)
                skills.append(
                    {
                        "name": entry.name,
                        "description": parsed["description"],
                        "author": parsed["author"],
                        "updated_at": skill_file.stat().st_mtime,
                    }
                )
    skills.sort(key=lambda s: s["updated_at"], reverse=True)
    return {"skills": skills}


@app.get("/api/skills/{name}")
def get_skill(name: str):
    path = skill_dir(name) / "SKILL.md"
    if not path.exists():
        raise HTTPException(404, f"No skill named '{name}'.")
    parsed = read_skill_md(path)
    return {
        "name": name,
        "description": parsed["description"],
        "author": parsed["author"],
        "body": parsed["body"],
    }


@app.post("/api/skills")
def create_skill(skill: SkillCreate):
    validate_skill_name(skill.name)
    path = skill_dir(skill.name)
    if path.exists():
        raise HTTPException(409, f"A skill named '{skill.name}' already exists.")
    path.mkdir(parents=True)
    write_skill_md(path / "SKILL.md", skill.name, skill.description, skill.body, skill.author)
    return {"name": skill.name, "restart_required": True}


@app.put("/api/skills/{name}")
def update_skill(name: str, skill: SkillUpdate):
    path = skill_dir(name)
    skill_file = path / "SKILL.md"
    if not skill_file.exists():
        raise HTTPException(404, f"No skill named '{name}'.")
    write_skill_md(skill_file, name, skill.description, skill.body, skill.author)
    return {"name": name, "restart_required": True}


@app.delete("/api/skills/{name}")
def delete_skill(name: str):
    path = skill_dir(name)
    if not path.exists():
        raise HTTPException(404, f"No skill named '{name}'.")
    shutil.rmtree(path)
    return {"name": name, "restart_required": True}


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

    custom = {
        k: v for k, v in (ticket.get("custom_fields") or {}).items()
        if v not in (None, "", [])
    }
    return {
        "id": ticket.get("id"),
        "subject": ticket.get("subject"),
        "description_text": ticket.get("description_text"),
        "status": ticket.get("status"),
        "priority": ticket.get("priority"),
        "tags": ticket.get("tags"),
        "created_at": ticket.get("created_at"),
        "updated_at": ticket.get("updated_at"),
        "custom_fields": custom,
        # Convenience only. The failure WINDOW is deliberately not computed
        # here: freshdesk-mcp's rca_hints already resolves it server-side
        # (preferring the operator's override window, else the reporter's
        # own "Report Time:" line, with proper zone conversion), and the
        # agent reads it from there. Doing date maths in the browser is
        # what produced a five-hour-wrong window before.
        "robot": custom.get("cf_robot_name"),
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
