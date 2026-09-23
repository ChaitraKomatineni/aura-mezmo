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
     noise filters applied PER APP AND LEVEL rather than by dropping a
     level globally -- fastloop loses DEBUG and INFO, taskloop loses
     DEBUG, pickle_rosbridge loses INFO, and 13 housekeeping apps are
     gated to problems only. Together that is 120.8M raw lines/day down
     to 39.9M. DEBUG from path_planning, action_planning and vision is
     deliberately KEPT: that is where those subsystems reason, and a
     global DEBUG rule was discarding 28M diagnostic lines a day to
     silence two chatty loops.
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
import copy
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

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

# Levels dropped for EVERY app. DEBUG is ~80% of fleet volume and, having
# gone through it app by app, the parts worth keeping were not worth the
# cost: at 10,807,838 lines/day a fleet-wide hour sits comfortably inside
# Mezmo's 1,000,000-line query ceiling, whereas keeping planner DEBUG put
# it at ~1.7M an hour and had the expensive tools rejected outright.
#
# For the record, since this was arrived at by measurement rather than
# assumption -- DEBUG by app, fleet-wide, Fri 2026-09-18:
#
#     fastloop          63,312,922
#     path_planning     13,075,160
#     action_planning   12,128,697
#     vision             3,121,594
#     taskloop           1,527,692
#     camera / navigation / api-server / safety_interface /
#     scan_perception / motor_controller             ~700k combined
#
# The consequence to keep in mind is that nothing explains itself at DEBUG
# any more. If a WARN/ERROR alone does not account for something, the
# answer is not in this view at all -- config/aura.toml tells the agent to
# say so rather than to infer from absence.
GLOBAL_LEVEL_EXCLUSIONS = ("debug",)

# Additional levels dropped for specific apps, on top of the global rule.
# Only INFO entries remain: DEBUG is handled globally above, so the
# taskloop and vision entries that used to live here would now be dead
# weight and are gone. These three are high-rate loop telemetry whose INFO
# is not events -- measured INFO volume fleet-wide on the same day:
#
#     fastloop         5,095,097   (an 83Hz control loop)
#     action_planning  5,737,545
#     path_planning      462,788
#
# NOTE for fastloop and action_planning: with both DEBUG and INFO gone,
# almost nothing of either survives -- roughly 5,000 and 45,000 lines a day
# respectively. config/aura.toml warns about both by name, so the agent
# reports "its routine logging is filtered out of this view" rather than
# concluding the subsystem was idle. path_planning keeps ~1.14M lines/day
# because much of its output carries no level at all.
#
# Matching is by prefix (Mezmo does this automatically), so `app:fastloop`
# would also catch a future `fastloop2`. There is no fastloop2 in
# production today -- `host:gen1-prod app:fastloop2` returns 0 -- but if one
# ships, it inherits this suppression without anyone deciding that.
APP_LEVEL_EXCLUSIONS = {
    "fastloop": ("info",),
    "path_planning": ("info",),
    "action_planning": ("info",),
}

# Apps removed in full, at every level. This is stronger than the
# problems-only gate below, which keeps ERROR/FATAL: nothing at all from
# these apps reaches the agent.
#
# pickle_rosbridge measured fleet-wide on Fri 2026-09-18 -- 2,839,922
# lines/day, of which 1,478,661 INFO, 13,214 WARN, 10,253 DEBUG, 466 ERROR,
# 6 FATAL/CRITICAL and ~1.34M carrying no level at all. It runs Foxglove /
# ROS diagnostics and the mobile-base LIDAR drivers, i.e. a visualisation
# and bridging layer rather than robot behaviour.
#
# The 466 errors and 6 fatals a day go too, and that is the deliberate
# difference from LEVEL_GATED_APPS. If a rosbridge fault ever needs
# investigating it will be invisible here and has to be queried outside
# this proxy -- config/aura.toml tells the agent to say so rather than
# report that there were none.
#
# dill-user is excluded for a completely different reason: not volume, but
# because it is a FALSE LEAD that reliably produces confident wrong answers.
#
# Measured over 30 days: 1,207 lines total, 598 at FATAL and 609 rendering
# the same payload unleveled. Every single one is an operator pressing the
# bug-report button -- `(User Submitted Bug Report) site: ..., operator:
# ..., description: ...`. There is no operational content in the app at all.
#
# The danger is its position. It arrives as FATAL, and the Freshdesk RCA
# method starts by walking severity top-down from FATAL, so the first thing
# the agent finds in the window is the report ITSELF -- which postdates the
# fault, describes it in the operator's words, and looks like the most
# severe event present. Aura duly reported it as the top critical event on
# ticket #8055.
#
# It is also redundant. Checked field by field against ticket #8055: the
# ticket's rca_hints already carries reported_at_utc (2026-09-23T17:35:02Z,
# identical to the log line's timestamp), the robot, the subsystem, the
# resolved window and cf_release_version. Freshdesk is the system of record
# for operator reports; this app is a copy of it that happens to look like a
# fatal log event.
#
# KNOWN LOSS, accepted: the log line carries `most_recent_bag` (e.g.
# 2026-09-23T13:34:22-04:00_recover_failed_mode.bag) and `operator`, and
# NEITHER appears anywhere in the Freshdesk ticket -- a .bag filename search
# across the whole ticket returns nothing. The bag name is the robot's own
# label for the state it was in, which is a genuine lead. It is given up
# because it is a lead rather than a window, its usefulness is unmeasured
# (n=1), and the ticket already supplies an equally precise report time. If
# the bag name ever proves to matter, the right fix is to enrich
# freshdesk-mcp's rca_hints with it, not to un-filter an app that reads as
# the fault.
FULLY_EXCLUDED_APPS = ("pickle_rosbridge", "dill-user")


# Apps that are operationally irrelevant to robot/arm/safety triage and are
# therefore gated down to problems only: VPN peer-discovery chatter, ROS
# demo nodes, kernel/audit/auth housekeeping, SLAM and bag-recording
# lifecycle, the metrics offload watcher, and the third-party OTA agent.
# Everything from these apps is dropped UNLESS it carries one of
# KEEP_LEVELS.
#
# Measured on Fri 2026-09-18 against the stream that actually reaches the
# agent (i.e. after the production + DEBUG + fastloop-INFO scope): these
# 13 apps were 3,136,442 of 13,935,928 lines -- 22.5% -- of which only
# 11,485 were error-ish. So this removes ~22.4% of what the agent sees.
# Against RAW fleet volume the same apps are only 2.6%; they loom much
# larger once the bulk application logging has already been filtered out.
#
# IMPORTANT CONSEQUENCE, deliberate but worth knowing: `audit`, `kernel`,
# `kern.log` and `auth.log` carry NO `level` field at all (measured: 0
# lines with level:* across a full day), so this rule removes them
# ENTIRELY rather than gating them. That is ~2.83M lines/day and it is
# the intended outcome -- the dominant cluster there is a cadvisor ptrace
# denial repeating up to 17/sec, a real but separate container-permissions
# issue -- but it does mean the agent cannot see kernel or audit events at
# all. Remove an app from this tuple if that ever needs revisiting.
#
# `cartographer_node`, `rosbag` and `foxglove_bridge` return zero lines on
# production hosts today; they are listed so the rule still holds if those
# subsystems start shipping from prod later.
LEVEL_GATED_APPS = (
    "tailscaled.service", "talker", "echoer",
    "audit", "kernel", "kern.log", "ssh.service", "auth.log",
    "cartographer_node", "rosbag", "foxglove_bridge",
    "user@1000.service", "miru.service",
)

# Severities worth keeping from the apps above. Mezmo matches values
# case-insensitively and by prefix, so `level:err` also covers ERR/error
# variants -- both spellings are listed anyway because the fleet emits a
# mix of cased and lowercase level values.
KEEP_LEVELS = ("error", "critical", "fatal", "alert", "err")


def _app_term(app: str) -> str:
    """Quote app values containing punctuation. Unquoted, a value like
    `user@1000.service` or `kern.log` risks tokenising on the punctuation
    rather than matching as one term."""
    return f'app:"{app}"' if any(c in app for c in "._@-") else f"app:{app}"


def _level_gate_clause(apps: tuple[str, ...], levels: tuple[str, ...]) -> str:
    """`-((app:a OR app:b) -(level:error OR ...))` -- drop everything from
    these apps except the listed severities.

    This is a negation wrapping a negation, which Mezmo does not document.
    Verified live rather than assumed: on Fri 2026-09-18 the scoped stream
    was 13,935,928 lines, these apps 3,136,442 of them, 11,485 error-ish,
    so the expected result was 10,810,971 -- and this clause returned
    exactly that. Two other formulations (a De Morgan
    `(-(apps) OR (levels))` form, and one listing each `-level:` term
    separately) returned the identical count; this one is used for being
    the shortest and closest to the intent.
    """
    if not apps or not levels:
        return ""
    app_group = "(" + " OR ".join(_app_term(a) for a in apps) + ")"
    keep = "(" + " OR ".join(f"level:{lv}" for lv in levels) + ")"
    return f"-({app_group} -{keep})"


# Specific, known-benign messages that are suppressed even though they are
# ERROR level. This list is different in kind from the structural filters
# above -- those drop whole categories, these drop one known message -- so
# it needs stricter discipline:
#
#   * Each entry requires ALL its terms, so it fails SAFE. If the message
#     wording changes, the suppression simply stops matching and the error
#     becomes visible again. The opposite (a loose single-word match) would
#     silently hide a genuinely different fault from the same subsystem.
#   * Each entry carries why it is benign, so the next person can judge
#     whether it still is.
#   * The agent is told these exist (see config/aura.toml) so it never
#     reports a clean bill of health when the only thing it can see has
#     been filtered.
#
# The point is not volume, it is keeping the ERROR tier trustworthy: the
# Freshdesk RCA method walks severity top-down and reads ERROR first, so
# a few thousand benign errors a day is exactly the wrong noise.
KNOWN_BENIGN = (
    {
        "label": "temperature-probe: sensor not connected",
        "why": (
            "The PCsensor TEMPerX232 USB probe is deliberately not fitted on "
            "these robots, so the reader logs ENOENT on /dev/serial/by-id/... "
            "on every poll. Expected, not a fault."
        ),
        # Measured Fri 2026-09-18: 3,133 lines/day, 31% of everything that
        # survives from user@1000.service. `"temperature-probe"` with
        # `-"Is sensor connected"` returned 0, i.e. every temperature-probe
        # line in the fleet is this one message -- so this is precise, not
        # merely convenient. All three terms are required anyway.
        "terms": ('app:"user@1000.service"', '"temperature-probe"',
                  '"Is sensor connected"'),
    },
)


def _known_benign_clause(entries) -> str:
    """One `-(term term term)` per known-benign message, ANDed together."""
    return " ".join(
        "-(" + " ".join(e["terms"]) + ")" for e in entries if e.get("terms")
    )


def _global_level_clause(levels: tuple[str, ...]) -> str:
    """`-level:debug` -- dropped for every app."""
    return " ".join(f"-level:{lv}" for lv in levels)


def _full_exclusion_clause(apps: tuple[str, ...]) -> str:
    """`-app:x -app:y` -- drop these apps entirely, every level."""
    return " ".join(f"-{_app_term(a)}" for a in apps)


def _app_level_clause(exclusions: dict[str, tuple[str, ...]]) -> str:
    """One `-(app:X (level:a OR level:b))` per app, ANDed together.

    Negating a *group* isn't something Mezmo documents (its docs promise `-`
    on a term, phrase, or field filter only), so the shape was verified
    against the live account rather than assumed: on a pinned 8h window the
    baseline was 11,825,178 lines and the INFO to be removed 5,879,250, and
    the clause returned exactly 5,945,928 -- the difference. Three
    formulations agreed to the line, including a De Morgan
    `(-level:info OR (-app:X -app:Y))` form; this one is used because it
    scales cleanly as apps and levels are added to the dict above.
    """
    parts = []
    for app, levels in exclusions.items():
        if not levels:
            continue
        lv = " OR ".join(f"level:{lv}" for lv in levels)
        wrapped = f"({lv})" if len(levels) > 1 else lv
        parts.append(f"-({_app_term(app)} {wrapped})")
    return " ".join(parts)


# AND-ed into every query. Order is irrelevant (whitespace is AND in Mezmo's
# syntax). Measured fleet-wide on Fri 2026-09-18:
#   host:gen1-prod                        -> 120,767,854  (raw)
#   ... plus the per-app level clause     ->  ~9,300,000
#   ... plus the level-gate clause        ->    8,543,428  (7.1% of raw)
#
# That averages ~356,000 lines/hour fleet-wide against Mezmo's
# 1,000,000-line query ceiling, but the average is the wrong number to
# reason from: the busiest measured hour (14:00-15:00 on that day) is
# 1,007,004 lines and IS still rejected with "Query Too Large" -- 0.7%
# over. So a fleet-wide hour sits exactly on the boundary: it works in
# quiet hours and fails in busy ones. Scoping to one robot is what makes
# `deduplicate_*`, `analyze_logs_for_root_cause_*` and
# `get_correlated_timeline_*` reliable, and the system prompt says so.
#
# Separately, and easy to mistake for the cap: these three tools also fail
# intermittently with "SSE stream ended without a response" regardless of
# volume -- a 5-minute single-robot window hits it. That is the documented
# TRANSIENT_SSE_DROP from experiments/mcp-tool-behavior and the remedy is
# to retry the identical arguments, NOT to narrow the query. Reading one
# as the other leads to narrowing a query that was never too large.
SCOPE_CLAUSE = " ".join(
    part
    for part in (
        f"host:{PROD_HOST_PREFIX}",
        _global_level_clause(GLOBAL_LEVEL_EXCLUSIONS),
        _full_exclusion_clause(FULLY_EXCLUDED_APPS),
        _app_level_clause(APP_LEVEL_EXCLUSIONS),
        _level_gate_clause(LEVEL_GATED_APPS, KEEP_LEVELS),
        _known_benign_clause(KNOWN_BENIGN),
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
    "group_logs_by_field",
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

# Exposed WITH A CAVEAT:
#   group_logs_by_field       - exposed on request, but its numbers are
#                               ordinal only. Measured: its `total` ran
#                               2.28x the real line count, and grouping by
#                               `app` summed to 4.4x (one bucket reported
#                               pct 108.3). "Which robot is noisiest" is
#                               trustworthy; "how many lines" is not, and
#                               config/aura.toml tells the agent so. It is
#                               kept because it is one of only two log
#                               tools not subject to the 1,000,000-line
#                               cap, which makes it the cheap way to find
#                               where to look before spending a capped
#                               tool on it.
#
# Deliberately NOT exposed, and why:
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
HOST_CLAUSE_RE = re.compile(r'(?<![\w-])host:(?:==)?("[^"]*"|[^\s()"]+)', re.IGNORECASE)

# Mezmo prefix-matches string fields automatically, so `host:gen1-prod2`
# silently also returns gen1-prod20, gen1-prod22, gen1-prod23... On a real
# day that is 39,940,404 lines when the robot itself logged 15,422,202 --
# i.e. asking about one robot hands back nearly three robots' worth of
# data with nothing to indicate it. An agent investigating gen1-prod2 was
# repeatedly shown gen1-prod22's sessions and could not work out why.
#
# `==` forces an exact match. Verified two ways on Thu 2026-09-17:
# `host:==gen1-prod2` returned 15,422,202, matching exactly what you get
# by taking the prefix query and subtracting every sibling by hand.
# Quoting alone does NOT do it (38,330,631), nor does a single `=`.
#
# Rewriting is limited to values that name a SPECIFIC robot -- the prod
# prefix followed by digits. A bare `host:gen1-prod` stays a prefix,
# because that is the fleet-wide scope this proxy injects itself and
# turning it into `host:==gen1-prod` would match nothing at all.
# Anchored to start-of-string, whitespace or an open paren, with an
# optional leading `-`. That covers negated clauses too -- `-host:gen1-prod2`
# left as a prefix would silently also exclude gen1-prod22, which is the
# same data-loss bug pointing the other way -- while still not matching
# `myhost:` or `my-host:`.
SPECIFIC_HOST_RE = re.compile(
    r'(?:(?<=^)|(?<=[\s(]))(-?)host:(?!==)"?(' + PROD_HOST_PREFIX + r'\d+[a-z]?)"?',
    re.IGNORECASE,
)


def exactify_hosts(query: str) -> str:
    """`host:gen1-prod2` -> `host:==gen1-prod2`, leaving `host:gen1-prod`
    (the fleet-wide prefix) alone. Negation is preserved."""
    return SPECIFIC_HOST_RE.sub(lambda m: f"{m.group(1)}host:=={m.group(2)}", query)

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
    # Tolerate the `==` exact-match prefix, which may already be present
    # on a caller's clause or added by exactify_hosts.
    cleaned = value.strip().strip('"').lstrip("=").strip('"').lower()
    return cleaned.startswith(PROD_HOST_PREFIX)


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
    query = exactify_hosts((query or "").strip())
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


def enrich_current_time(result):
    """Add weekday names to get_current_time's response.

    Upstream returns only ISO instants -- {"now", "one_hour_ago",
    "one_day_ago"} -- with no weekday anywhere. That leaves the agent doing
    calendar arithmetic in its head to resolve "this past Friday", and it
    gets it wrong: asked for Friday it queried 2026-09-19 (a Saturday),
    found zero logs, and reported the robot as down for a day it had in
    fact logged 24 million lines.

    So resolve the calendar here, where it is a library call rather than a
    guess. `weekday_to_date` is the direct answer to "this past <day>" and
    is what makes the lookup arithmetic-free.
    """
    payload = next((p for p in response_payloads(result) if isinstance(p, dict)), None)
    blocks = list(getattr(result, "content", None) or [])
    if payload is None or not blocks or not hasattr(blocks[0], "text"):
        return result
    try:
        now = datetime.fromisoformat(str(payload.get("now")).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return result

    day = lambda n: now - timedelta(days=n)  # noqa: E731
    # Most recent occurrence of each weekday, today included. Offset 0-6
    # from today covers exactly one of each.
    weekday_to_date = {day(i).strftime("%A"): day(i).strftime("%Y-%m-%d") for i in range(7)}

    payload = dict(payload)
    payload.update({
        "today": now.strftime("%Y-%m-%d"),
        "weekday": now.strftime("%A"),
        "yesterday": day(1).strftime("%Y-%m-%d"),
        "last_14_days": {day(i).strftime("%Y-%m-%d"): day(i).strftime("%A")
                         for i in range(14)},
        "weekday_to_date": weekday_to_date,
        "retention_earliest": day(30).strftime("%Y-%m-%d"),
        "note": (
            "Use weekday_to_date to resolve phrases like 'this past Friday' "
            "-- do not compute dates yourself. Every date is UTC. Queries "
            "before retention_earliest will be rejected."
        ),
    })

    enriched = blocks[0].model_copy(update={"text": json.dumps(payload)})
    return ToolResult(content=[enriched] + blocks[1:], structured_content=payload)


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
        arguments = unwrap_arguments(dict(arguments or {}))

        if self.name in SCOPED_TOOLS:
            offending = find_out_of_scope_host(arguments.get("query"))
            if offending is not None:
                raise ToolError(SCOPE_ERROR.format(host=offending))
            arguments["query"] = scope_query(arguments.get("query"))

        result = await call_upstream(self.name, arguments)
        check_response_scope(self.name, result)

        if self.name == "get_current_time" and not getattr(result, "is_error", False):
            return enrich_current_time(result)

        return ToolResult(
            content=list(getattr(result, "content", None) or []),
            structured_content=getattr(result, "structured_content", None),
            is_error=bool(getattr(result, "is_error", False)),
        )


# Upstream schema defects worth repairing before the agent ever sees them.
#
# `dedup_mode` DOES describe itself upstream, but as a `oneOf` of two
# `const` branches. That form survives the pipeline badly: fastmcp
# surfaces the property as type=None/enum=None, and the model evidently
# does not follow it either. A flat type+enum alongside the oneOf is
# understood by both. Observed in the wild before this patch, it emitted
#     "dedup_mode": none
# i.e. the bare Python literal, which is not valid JSON. The tool-call
# JSON then failed to parse and the framework fell back to passing the
# whole argument blob as a single string under a "result" key, so the
# server saw no from_time at all and rejected the call. The agent retried
# the same malformed call nine times.
#
# The error sweep (experiments/mcp-tool-behavior) already established the
# real domain: "unknown variant `exact`, expected `none` or `template`".
# Declaring that here means the model emits a quoted, valid value.
SCHEMA_PATCHES = {
    "get_correlated_timeline_time_range": {
        "dedup_mode": {"type": "string", "enum": ["none", "template"]},
    },
    "get_correlated_timeline_relative_time": {
        "dedup_mode": {"type": "string", "enum": ["none", "template"]},
    },
}


def patch_schema(tool_name: str, schema: dict) -> dict:
    """Apply SCHEMA_PATCHES, and warn about any other untyped property --
    an untyped parameter is the same landmine waiting to go off."""
    patches = SCHEMA_PATCHES.get(tool_name) or {}
    props = schema.get("properties") or {}
    if patches:
        schema = copy.deepcopy(schema)
        props = schema.setdefault("properties", {})
        for name, patch in patches.items():
            if name in props:
                props[name].update(patch)
                print(f"    patched {tool_name}.{name} -> {patch}", flush=True)
    for name, spec in props.items():
        if not spec.get("type") and not spec.get("enum") and "$ref" not in spec:
            print(f"    WARNING: {tool_name}.{name} has no type or enum; the "
                  "model will be guessing its shape", file=sys.stderr, flush=True)
    return schema


def unwrap_arguments(arguments: dict) -> dict:
    """Undo the framework's {"result": "<raw json text>"} fallback.

    When the model emits tool-call JSON that will not parse, Aura/rig hands
    the raw text through as a single `result` string rather than failing
    loudly, which surfaces here as a baffling "missing field `from_time`".
    Recover the real arguments where we can. The SCHEMA_PATCHES above are
    the actual fix -- this is a net under it, for the next malformed value
    we have not predicted.
    """
    if set(arguments) != {"result"} or not isinstance(arguments["result"], str):
        return arguments
    raw = arguments["result"]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Python literals leaking into JSON is the failure we actually saw.
        repaired = re.sub(r"(?<=:\s)(none|None)(?=\s*[,}])", "null", raw)
        repaired = re.sub(r"(?<=:\s)True(?=\s*[,}])", "true", repaired)
        repaired = re.sub(r"(?<=:\s)False(?=\s*[,}])", "false", repaired)
        try:
            parsed = json.loads(repaired)
        except json.JSONDecodeError:
            return arguments
    if not isinstance(parsed, dict):
        return arguments
    print(f"  recovered arguments from a malformed tool call: {sorted(parsed)}",
          file=sys.stderr, flush=True)
    # A null that came from a bare `none` means "the model meant the string
    # 'none'" for enum-ish fields; dropping it lets the server default
    # instead of rejecting a null.
    return {k: v for k, v in parsed.items() if v is not None}


class DescribeScopeTool(Tool):
    """Publishes the proxy's live scope instead of making the prompt carry a
    copy of it.

    The prompt used to restate the clause verbatim, and drifted: it claimed
    `-level:debug` was applied for weeks after that line was disabled, so
    the agent was told DEBUG was invisible while it was being returned.
    Filter rules and the description of the filter rules are now the same
    artifact, and there is nothing to keep in sync.
    """

    async def run(self, arguments: dict) -> ToolResult:
        payload = {
            "injected_clause": SCOPE_CLAUSE,
            "production_hosts": f"{PROD_HOST_PREFIX}* (exact-matched per robot)",
            "levels_excluded_for_every_app": list(GLOBAL_LEVEL_EXCLUSIONS),
            "apps_excluded_entirely": list(FULLY_EXCLUDED_APPS),
            "additional_app_level_exclusions": {
                a: list(lv) for a, lv in APP_LEVEL_EXCLUSIONS.items()
            },
            "apps_gated_to_problems_only": list(LEVEL_GATED_APPS),
            "levels_kept_for_gated_apps": list(KEEP_LEVELS),
            "suppressed_known_benign": [
                {"label": e["label"], "why": e["why"]} for e in KNOWN_BENIGN
            ],
            "tools_exposed": sorted(ALLOWLIST),
            "notes": [
                "Apps in apps_excluded_entirely are gone at EVERY level, "
                "including ERROR and FATAL -- stronger than the problems-only "
                "gate. You cannot see them at all.",
                "DEBUG is excluded for EVERY app. Nothing explains itself at "
                "DEBUG in this view -- if a WARN/ERROR does not account for "
                "something, say the detail is outside this view rather than "
                "inferring from its absence.",
                "Apps with no `level` field at all (audit, kernel, kern.log, "
                "auth.log) are removed entirely by the problems-only gate -- "
                "you cannot see them, which is not the same as them being empty.",
                "Mezmo rejects any query matching over 1,000,000 lines. Narrow "
                "to one robot before using deduplicate_*, "
                "analyze_logs_for_root_cause_* or get_correlated_timeline_*.",
            ],
        }
        return ToolResult(content=[], structured_content=payload,
                          meta={"text": json.dumps(payload, indent=2)})


def describe_scope_tool() -> DescribeScopeTool:
    return DescribeScopeTool(
        name="describe_scope",
        description=(
            "What filtering is applied to every query you send, verbatim and "
            "live: the injected clause, which apps lose which levels, which "
            "apps are gated to errors only, known-benign suppressions, and the "
            "volume cap. Call this instead of assuming what is filtered -- it "
            "is generated from the running configuration, so it cannot be "
            "out of date."
        ),
        # No `required` key at all. With `"required": []` present, Aura's
        # schema sanitiser (sanitize_schemas = true, "OpenAI compatibility")
        # silently drops the tool: the proxy listed 11, Aura registered 10
        # and logged nothing about the one it discarded. get_current_time,
        # which also takes no arguments, survives because its upstream
        # schema omits the key rather than sending it empty.
        parameters={"type": "object", "properties": {}},
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
        schema = patch_schema(name, input_schema_of(tool))

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
    # Local, not proxied: this one answers from our own configuration.
    mcp.add_tool(describe_scope_tool())

    # Derived, never a literal -- a hand-maintained tool count had already
    # drifted (three files said 9 while the allowlist held 10).
    print(
        f"Serving {len(proxies) + 1} tool(s) on port {PORT} "
        f"| {len(proxies)} proxied + describe_scope "
        f"| tripwire: {TRIPWIRE_MODE}",
        flush=True,
    )
    print(f"  scope: {SCOPE_CLAUSE}", flush=True)
    for entry in KNOWN_BENIGN:
        print(f"  suppressed (known-benign): {entry['label']} -- {entry['why']}",
              flush=True)
    mcp.run(transport="streamable-http", host="0.0.0.0", port=PORT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
