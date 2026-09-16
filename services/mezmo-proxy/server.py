"""
Mezmo Proxy MCP Server

Sits between the Aura agent and Mezmo's hosted MCP server
(https://mcp.mezmo.com/mcp) and enforces, in code the agent never sees and
cannot argue its way around:

  1. A TOOL ALLOWLIST. Mezmo exposes 32 tools on this account, ~20 of which
     mutate pipelines (create/publish/pause/delete, mint access keys). Only
     the read-only log tools below are re-exposed; everything else simply
     does not exist from Aura's point of view.
  2. A PRODUCTION-ONLY SCOPE. Every query is rewritten to AND in
     `host:gen1-prod`, so only production robots are ever searched, plus
     two noise filters: `-level:debug` (~80% of line volume, measured) and
     suppression of INFO from the two highest-volume apps (a further ~50%).
  3. A RESPONSE TRIPWIRE. Responses are checked for structural `host`
     fields outside scope, in case an upstream tool ever ignores `query`.

Aura must NOT hold MEZMO_API_KEY -- this server holds it instead. Otherwise
the filtering above is advisory: the agent could reach mcp.mezmo.com itself
and bypass the proxy entirely. See the `aura` service in docker-compose.yml,
which explicitly blanks the key out of that container's environment.

Tool names, descriptions and input schemas are discovered live from Mezmo at
startup and re-exposed verbatim rather than being redeclared here. That
matters: Mezmo's `query` parameter description carries its whole query-syntax
guide (fielded search, negation, grouping, the fact that `*` wildcards are
NOT supported), and the agent needs that text to build valid queries.
"""

import asyncio
import json
import os
import re
import sys

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool, ToolResult

MEZMO_URL = os.environ.get("MEZMO_MCP_URL", "https://mcp.mezmo.com/mcp")
API_KEY = os.environ.get("MEZMO_API_KEY", "")
PORT = int(os.environ.get("PORT", "8093"))

# Production hosts are exactly those whose name begins with this prefix.
# Verified against the live account: `gen1-prod` prefix-matches gen1-prod1,
# gen1-prod2, gen1-prod34 ... and does NOT match gen1-dev9 (a real 900k
# line/8h dev robot) or gen1-proto3 -- "gen1-prod" and "gen1-proto3" diverge
# at d/t, so the prototype is excluded by the prefix itself, not just by the
# request check below. A bare `gen1-` prefix would have swept both in.
#
# Note there is deliberately no wildcard here: Mezmo's own query docs state
# `*` is NOT supported in field values and that prefix matching is automatic,
# so `host:gen1-prod*` is invalid syntax -- `host:gen1-prod` is the correct
# form and already matches every gen1-prod* host.
PROD_HOST_PREFIX = "gen1-prod"

# Apps whose INFO-level chatter is suppressed. These two are the fleet's
# highest-volume talkers and their INFO lines are routine loop telemetry,
# not events worth an agent's context. Measured over a pinned 8h window:
#   fastloop         INFO 5,095,097 of 5,098,577 non-debug lines (99.93%)
#   pickle_rosbridge INFO   784,153 of 1,506,263 non-debug lines (52.1%)
#
# NOTE the consequence for fastloop: because essentially all of its
# non-debug output is INFO, suppressing INFO removes fastloop from the
# agent's view almost entirely (3,480 lines survive in that window). The
# system prompt in config/aura.toml says so explicitly, so the agent
# reports "fastloop's routine logging is filtered out here" instead of
# concluding fastloop was idle.
#
# Matching is by prefix (Mezmo does this automatically), so `app:fastloop`
# would also catch a future `fastloop2`. There is no fastloop2 in
# production today -- `host:gen1-prod app:fastloop2` returns 0 -- but if one
# ships, it inherits this suppression without anyone deciding that.
INFO_SUPPRESSED_APPS = ("fastloop", "pickle_rosbridge")


def _info_noise_clause(apps: tuple[str, ...]) -> str:
    """`-(level:info (app:a OR app:b))` -- exclude INFO, but only from these
    apps; their WARN/ERROR/FATAL lines are still returned.

    Negating a *group* isn't something Mezmo documents (its docs promise `-`
    on a term, phrase, or field filter only), so this was verified against
    the live account rather than assumed. On a pinned 8h window the baseline
    was 11,825,178 lines and the INFO to be removed 5,879,250; this clause
    returned exactly 5,945,928 = the difference. Three other formulations
    -- two flat `-(level:info app:X)` clauses, and the De Morgan
    `(-level:info OR (-app:X -app:Y))` form -- returned the identical count,
    so the grouped version is chosen for being the one that scales cleanly
    as apps are added to the tuple above.
    """
    if not apps:
        return ""
    return "-(level:info (" + " OR ".join(f"app:{a}" for a in apps) + "))"


# AND-ed into every query. Order is irrelevant (whitespace is AND in Mezmo's
# syntax). Each clause verified live against get_log_histogram:
#   host:gen1-prod                        -> 9,166,715 lines / 8h
#   host:gen1-prod -level:debug           -> 1,778,362 lines / 8h  (-80.6%)
#   ... plus the INFO-noise clause        -> a further -49.7%
SCOPE_CLAUSE = " ".join(
    part
    for part in (
        f"host:{PROD_HOST_PREFIX}",
        "-level:debug",
        _info_noise_clause(INFO_SUPPRESSED_APPS),
    )
    if part
)

# Tools that read log data. Each MUST declare a `query` parameter -- that's
# the only channel the scope can be injected through, so a tool listed here
# without one is a configuration error and refused at startup rather than
# silently registered unscoped.
SCOPED_TOOLS = {
    "deduplicate_logs_relative_time",
    "deduplicate_logs_time_range",
    "analyze_logs_for_root_cause_relative_time",
    "analyze_logs_for_root_cause_time_range",
    "get_correlated_timeline_relative_time",
    "get_correlated_timeline_time_range",
    "get_log_histogram",
}

# Tools that take no query because they return no log data at all -- pure
# time arithmetic. Safe to pass through unscoped, and the agent needs them
# to turn "yesterday afternoon" into the absolute bounds the _time_range
# tools require.
UNSCOPED_TOOLS = {
    "get_current_time",
    "relative_time_to_time_range",
}

ALLOWLIST = SCOPED_TOOLS | UNSCOPED_TOOLS

# Deliberately NOT exposed, and why:
#   group_logs_by_field       - its buckets summed to 1.96x its own stated
#                               total on a real run (one bucket reported
#                               pct 108.3), so its numbers can't be trusted
#                               in front of a customer yet.
#   list_log_fields           - takes no query, so it cannot be scoped; also
#                               returned 107 KB / 4,430 fields in one call.
#   tap_pipeline_component    - taps live pipeline data with no query
#                               parameter, i.e. unscopeable raw data.
#   get_ai_investigation,
#   list_ai_investigations    - no query; may surface investigations that
#                               were run over dev hosts.
#   list_exclusion_rules      - no query; account-wide config.
#   ~20 pipeline mutators     - create/publish/pause/delete pipelines and
#                               components, create_pipeline_access_key.
#                               Read-only proxy; nothing here changes
#                               existing pipelines.

# Matches a positive `host:` clause and captures its value. The lookbehind
# excludes `-host:` (a negated host filter only ever narrows, so rejecting
# it would be wrong) and identifiers that merely end in "host", e.g.
# `_host:` or `myhost:`, since [\w-] covers both the '-' and word cases.
HOST_CLAUSE_RE = re.compile(r'(?<![\w-])host:("[^"]*"|[^\s()"]+)', re.IGNORECASE)

# error | warn | off -- what to do when the tripwire sees an out-of-scope
# host in a response. Defaults to error; downgrade to warn if a legitimate
# production query ever trips it because a log line's own structured payload
# happened to carry a `host` field naming another machine.
TRIPWIRE_MODE = os.environ.get("TRIPWIRE_MODE", "error").lower()

SCOPE_ERROR = (
    "Host {host!r} is outside this proxy's scope.\n"
    "This proxy serves PRODUCTION robots only -- host names beginning with "
    f"'{PROD_HOST_PREFIX}' (gen1-prod1, gen1-prod2, gen1-prod17, ...).\n"
    "Development robots (gen1-dev*), prototypes (gen1-proto*), and all "
    "non-gen1 hosts are not available here.\n"
    "Re-run the query against a production robot, or omit the host filter "
    "to search the whole production fleet."
)


def is_production_host(value: str) -> bool:
    return value.strip().strip('"').lower().startswith(PROD_HOST_PREFIX)


def find_out_of_scope_host(query: str | None) -> str | None:
    """First positive `host:` value in `query` that isn't a production host,
    or None. Best-effort by design: this exists to produce a useful error
    message, NOT to enforce the boundary -- enforcement is scope_query()'s
    unconditional AND, which holds even if this misses an exotic query
    shape. Detection is advisory; the AND is the guarantee."""
    if not query:
        return None
    for match in HOST_CLAUSE_RE.finditer(query):
        value = match.group(1)
        if not is_production_host(value):
            return value.strip('"')
    return None


def scope_query(query: str | None) -> str:
    """AND the production scope onto a caller-supplied query.

    The caller's query is wrapped in parentheses. Measured against the live
    account, Mezmo applies a trailing clause to the whole expression even
    without them -- `app:fastloop OR host:gen1-dev9 host:gen1-prod` returned
    4,359,817 lines, matching the wrapped form (4,359,434, the difference
    being live volume drift between the two calls) and clearly distinct from
    unscoped `app:fastloop` (5,233,349). So OR does not escape the filter
    here today. The parentheses are kept anyway: they cost nothing, make the
    intent explicit, and mean this does not silently become bypassable if
    Mezmo's parser precedence ever changes.
    """
    query = (query or "").strip()
    if not query:
        return SCOPE_CLAUSE
    return f"({query}) {SCOPE_CLAUSE}"


def iter_host_values(node):
    """Yield values of keys literally named `host`/`_host` anywhere in a
    parsed response. Only structural host fields are inspected -- free text
    is never scanned, so a log message that merely mentions a dev hostname
    can't trip the wire."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in ("host", "_host") and isinstance(value, str):
                yield value
            else:
                yield from iter_host_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_host_values(item)


def response_payloads(result):
    """Best-effort JSON views of an upstream result, for the tripwire."""
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        yield structured
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            yield json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue


def check_response_scope(tool_name: str, result) -> None:
    if TRIPWIRE_MODE == "off":
        return
    for payload in response_payloads(result):
        for host in iter_host_values(payload):
            if is_production_host(host):
                continue
            message = (
                f"TRIPWIRE: {tool_name} returned out-of-scope host {host!r} "
                "despite the injected production scope."
            )
            if TRIPWIRE_MODE == "error":
                print(message, file=sys.stderr, flush=True)
                raise ToolError(
                    f"Upstream returned data for out-of-scope host {host!r}; "
                    "response withheld. This indicates the production scope "
                    "was not applied upstream -- please report it."
                )
            print("WARNING: " + message, file=sys.stderr, flush=True)
            return


async def call_upstream(tool_name: str, arguments: dict):
    """Forward one call to Mezmo under this server's own credentials.

    A fresh session per call: the handshake is negligible next to a
    multi-second log query, and it avoids holding a long-lived upstream
    session that can go stale between sporadic triage questions.
    """
    transport = StreamableHttpTransport(
        url=MEZMO_URL, headers={"Authorization": f"Bearer {API_KEY}"}
    )
    async with Client(transport) as client:
        return await client.call_tool(tool_name, arguments, raise_on_error=False)


class ProxiedTool(Tool):
    """One allowlisted upstream tool, re-exposed with its original name,
    description and input schema. `name` is the upstream name, so no extra
    field is needed to route the call; whether to scope is decided by
    membership in SCOPED_TOOLS."""

    async def run(self, arguments: dict) -> ToolResult:
        arguments = dict(arguments or {})

        if self.name in SCOPED_TOOLS:
            offending = find_out_of_scope_host(arguments.get("query"))
            if offending is not None:
                raise ToolError(SCOPE_ERROR.format(host=offending))
            arguments["query"] = scope_query(arguments.get("query"))

        result = await call_upstream(self.name, arguments)
        check_response_scope(self.name, result)

        return ToolResult(
            content=list(getattr(result, "content", None) or []),
            structured_content=getattr(result, "structured_content", None),
            is_error=bool(getattr(result, "is_error", False)),
        )


def input_schema_of(tool) -> dict:
    """mcp SDK v2 renamed inputSchema -> input_schema; read whichever this
    client version provides."""
    return getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}


async def discover_tools() -> list[ProxiedTool]:
    """Fetch Mezmo's live tool list and build proxies for the allowlist."""
    transport = StreamableHttpTransport(
        url=MEZMO_URL, headers={"Authorization": f"Bearer {API_KEY}"}
    )
    async with Client(transport) as client:
        upstream = await client.list_tools()

    by_name = {t.name: t for t in upstream}
    print(f"Upstream exposes {len(by_name)} tool(s); allowlist has {len(ALLOWLIST)}", flush=True)

    missing = sorted(ALLOWLIST - by_name.keys())
    if missing:
        print(f"WARNING: allowlisted tools absent upstream: {missing}", file=sys.stderr, flush=True)

    proxies = []
    for name in sorted(ALLOWLIST & by_name.keys()):
        tool = by_name[name]
        schema = input_schema_of(tool)

        # A scoped tool with no `query` parameter could not be constrained,
        # so refuse it rather than register something unenforceable.
        if name in SCOPED_TOOLS and "query" not in (schema.get("properties") or {}):
            print(
                f"REFUSING {name}: listed as scoped but declares no `query` "
                "parameter, so the production scope cannot be injected.",
                file=sys.stderr,
                flush=True,
            )
            continue

        proxies.append(
            ProxiedTool(
                name=name,
                title=getattr(tool, "title", None),
                description=tool.description,
                parameters=schema,
            )
        )
        print(f"  + {name}{' (scoped)' if name in SCOPED_TOOLS else ''}", flush=True)

    return proxies


def main() -> int:
    if not API_KEY:
        print(
            "ERROR: MEZMO_API_KEY is not set. This proxy holds the Mezmo "
            "credential on the agent's behalf and cannot start without it.",
            file=sys.stderr,
        )
        return 1

    if TRIPWIRE_MODE not in ("error", "warn", "off"):
        print(f"ERROR: TRIPWIRE_MODE must be error|warn|off, got {TRIPWIRE_MODE!r}", file=sys.stderr)
        return 1

    print(f"Connecting to {MEZMO_URL} to discover tools...", flush=True)
    try:
        proxies = asyncio.run(discover_tools())
    except Exception as exc:  # noqa: BLE001 -- fail fast with one clear line
        print(f"ERROR: could not reach upstream MCP: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not proxies:
        # Starting with zero tools would pass the container healthcheck (a
        # bare MCP initialize still succeeds) while being useless, so treat
        # it as a hard failure and let docker restart us.
        print("ERROR: no allowlisted tools could be registered.", file=sys.stderr)
        return 1

    mcp = FastMCP("mezmo-proxy")
    for proxy in proxies:
        mcp.add_tool(proxy)

    print(
        f"Serving {len(proxies)} tool(s) on port {PORT} | scope: {SCOPE_CLAUSE} "
        f"| tripwire: {TRIPWIRE_MODE}",
        flush=True,
    )
    mcp.run(transport="streamable-http", host="0.0.0.0", port=PORT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
