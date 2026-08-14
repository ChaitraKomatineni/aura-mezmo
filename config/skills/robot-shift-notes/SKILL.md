---
name: robot-shift-notes
description: Use when asked for shift notes, a session summary, or a plain-English recap of what a Pickle Robot unit did during a session, based on Mezmo/robot logs.
---
# Robot Shift Notes

Produce a plain-English shift note for a robot's session, as if someone had
been watching both the physical robot and its logs and is now explaining
what happened to the next-shift operator. Write for a person, not an
engineer: name times, name events, skip jargon where a plain description
works instead. A shift note is a triage-ready summary, not a raw event dump
— see "Grouping vs. narrating" below before you start writing.

## Scope — the user gives a session label; find where it starts and stops

Shift notes are always for one specific session, and the user identifies
which one — never infer or guess which session they mean from a full log
file on your own.

1. **Require a session label before searching.** If the user hasn't given
   one, ask for it first rather than defaulting to the whole file or
   guessing which session they want.
2. **There is currently no confirmed keyword for a session's start/stop
   marker.** (`Received login request` was tried and does not work — don't
   use it.) Until a real one is confirmed, search using the label itself as
   a plain text filter to find where that session's activity appears in
   the logs, then treat the earliest matching timestamp as an *approximate*
   start and the latest as an *approximate* stop.
3. **Say explicitly that the start/stop times are approximate** based on
   the first/last matches for the label, not a confirmed session-boundary
   event — don't present them as exact. If the true start is likely earlier
   than the first match (e.g. setup/config activity before the label
   starts appearing), say that too rather than silently omitting it.
4. **If searching the label finds nothing**, say so and ask the user to
   double-check it — don't fall back to guessing by timestamp.
5. **Scope every other search in this skill to that approximate start/stop
   window** (e.g. add the time bound to `logs_search_logs` / `mezmo_*`
   calls). A single real session should rarely need the
   grouping-into-counts treatment described below, but apply it anyway if
   the session turns out to run unusually long or be unusually eventful.

**Open question — what does a "session label" actually look like, and is
there a real start/stop marker?** Neither is confirmed yet: it's unclear
whether the label is a literal token/ID that appears throughout a
session's log lines, a timestamp the user names informally, or something
else — and there's no known keyword yet for the actual moment a session
starts or ends. Flag this to the user in your summary if the approximation
in steps 2-3 seems like it might be missing real start/stop activity.

## Ground rules — read before searching

1. **Never invent a timestamp or event.** Every line in the timeline below
   must be backed by an actual log line you found via a tool call
   (`logs_search_logs`, `logs_read_log`, or the `mezmo_*` tools). If you
   can't find something, say "not found in the available logs" rather than
   guessing.
2. **Some keyword lookups below are marked `TODO` — not yet defined.** If the
   user needs one of those categories, tell them the keyword isn't
   configured yet rather than making one up. Log analysis on a physical
   robot is safety-relevant; a fabricated match is worse than an honest gap.
3. Search terms are given **without quotes on purpose** in most rows — some
   are prefixes of related keywords (e.g. `Current action is pick in mode
   durable` also needs to catch `very_durable_pick`). Only quote a term if
   the row explicitly shows quotes.
4. `host:` and `app:` prefixes shown in a row are filters to combine with the
   keyword, not separate searches — e.g. `host:gen1-prod17
   mode.out_of_moves` means "search for `mode.out_of_moves` scoped to host
   gen1-prod17". See "Search syntax" below for exact filter syntax.
5. **Don't assume an `app:` value from a container name.** The container
   glossary below (`dill_app_taskloop`, `dill_app_camera`, etc.) is *not*
   confirmed to map directly onto the `app` field's actual values — see the
   open question in that section. Only use an `app:` value that's already
   given explicitly in the keyword reference below; don't invent one from
   the container list.
6. **If a search returns a very large result, don't try to hold it all in
   context at once.** Use whatever exploration tools are available (grep,
   slice, head-style tools) to narrow it down, or issue a tighter follow-up
   search (add a time window or a more specific term) rather than reading
   the entire raw output.
7. **If you run out of tool-call budget before finishing the full sweep**,
   stop and write the note anyway with what you found, and say explicitly
   which categories you didn't get to check — never fail silently with no
   output at all. A partial, honestly-labeled note is more useful than none.
8. **Always write times in human-readable 12-hour format, never raw
   `HH:MM:SS`.** These notes are for a person to skim and get the picture
   quickly — "2:32 PM" reads instantly, "14:32:07" doesn't. Drop seconds;
   minute precision is enough for a shift note. Use the log's actual
   timestamp to *find* the second-level detail, but *display* the rounded,
   12-hour form. (Internally you can still reason in whatever precision the
   logs give you — this rule is about what appears in the written note.)
9. **If what's being asked doesn't match anything in the keyword reference
   below, don't refuse — explore, then say so plainly.** The keyword table
   covers known categories; it isn't exhaustive of everything a person might
   ask about a session. First check carefully whether the question actually
   does map to a category above (including a `TODO` one — that's a
   different case, covered by ground rule 2). Only fall back to this rule
   once you're sure it doesn't. When it doesn't: search the available logs
   directly using your own judgment — broad `logs_search_logs`/`mezmo_*`
   queries, and the scratchpad exploration tools if a result gets diverted
   there — rather than telling the user there's nothing you can do. Answer
   with whatever you actually find (ground rule 1 still applies — quote
   real log lines, never invent one), but say explicitly, in the note
   itself, that this particular finding is not based on a confirmed keyword
   or a hardcoded category — it's your own read of the raw logs, and it
   could be wrong in ways a keyword-backed finding wouldn't be. Don't blend
   this kind of finding in silently next to the keyword-backed ones without
   flagging which is which.
10. **Before presenting the note, verify it against this checklist**: every
    quoted log line actually exists in a tool result you called, not
    paraphrased from memory; every out-of-moves entry has a real
    package-spec lookup behind its number, not a guess; every time shown is
    12-hour human-readable, not raw `HH:MM:SS`; anything you didn't get to
    is listed under "Not checked," not silently dropped. Fix anything that
    fails this check before showing the note to the user.

## Grouping vs. narrating — this is what makes it a "shift note" and not a log dump

Don't give every matching log line its own timeline entry, especially over
a long window. Instead:

- **Narrate individually**: e-stops, box drops, aborted picks, interventions,
  operator/UI-driven changes (spec/mode changes), and anything that isn't
  the robot's normal operating pattern. These are why someone reads a shift
  note.
- **Roll up into a count**: routine successful cycles, ordinary planner
  activity, and repeated non-error status messages. Report these as a
  single line — e.g. "142 successful pick cycles between 6:00 AM and
  2:00 PM, averaging about 28 seconds each" — not one bullet per cycle.
- If the same anomaly repeats many times (e.g. 30 conveyor-blocked aborts),
  report it as one entry with a count and the time range, not 30 entries.

## Output format

```
# Shift Notes — <host/robot id if known> — <date/window covered>

**Session:** started <e.g. "2:14 PM">, ended <e.g. "5:02 PM" or "still active">
**Rosbag recording:** started <human time>, stopped <human time>
**Production version:** <version, if found>
**Out-of-moves events:** <count, or "none">

## Timeline
- <human time> — <notable individual event, in plain English>
  - If this is an out-of-moves event: Package specs enabled at the time: <N, or 0>
- <human time>–<human time> — <rolled-up routine activity, with a count>
...

## Not checked
<Only if you ran out of budget or a keyword is undefined: list which
categories weren't verified, so the reader knows the gaps.>

## Summary
<3-5 sentences: overall how the shift went, anything that needed
intervention, anything the next shift should watch for.>
```

Order the timeline chronologically. Fold config/version info in as the first
timeline entry rather than a separate section, unless the user only asked
for config info. Omit the "Not checked" section entirely if nothing was
skipped. See "Out-of-moves reporting" below for exactly how to fill in the
count and per-event package-spec numbers, and "Examples" below for a full
worked note.

## Out-of-moves (OOMV) reporting — always required, not optional

Whenever a session has any out-of-moves events (see `mode.out_of_moves` in
the keyword reference below), report on them in full every time — this
isn't conditional on the user asking specifically:

1. **Count every occurrence** of the out-of-moves keyword within the
   session window and put the total in the `**Out-of-moves events:**` line
   near the top of the note (write "none" if zero — don't omit the line).
2. **List each occurrence individually** in the Timeline (they're an
   anomaly, so this is already required by "Grouping vs. narrating" —
   don't collapse multiple out-of-moves events into one rolled-up count
   even if there are many).
3. **Under each individual occurrence**, look up the most recent
   package-specs log entry at or before that event's timestamp (search
   `app:vision active list of package_specs` OR `app:vision package
   specs`) and report how many specs were listed as "Package specs enabled
   at the time: N". If no package-specs entry exists before that
   out-of-moves event anywhere in the available logs, report `0`, don't
   omit the line.
4. **If the package-specs log line's format doesn't make counting entries
   straightforward** (e.g. it's not a clean list you can count), say so
   explicitly next to that occurrence rather than guessing a number.

## Examples

The examples below show the expected format and reasoning in practice. The
first is real, from an actual session analyzed in this project — its facts
are exactly what searching the real logs turned up, including its gaps. The
rest are illustrative: built to demonstrate a format or a rule, not drawn
from a real session. Never treat the illustrative examples as confirmed
facts about any real robot, host, or session.

<examples>

<example illustrative="false">
Real — session 08-03-26-D51-CPU-T-48-872587 on gen1-prod16, from the actual
logs analyzed in this project (host only exposed `taskloop`/`api-server` in
this export — see the gaps this note discloses below).

```
# Shift Notes — gen1-prod16 — Aug 3, 2026

**Session:** started 1:13 PM, ended 3:05 PM
**Rosbag recording:** not found in the available logs
**Production version:** not found in the available logs
**Out-of-moves events:** none found in the available data (see "Not checked" — this export doesn't include the apps that would carry that keyword)

## Timeline
- 1:13 PM — Operator set the session label via the UI (`POST request on /api/workload_label`, `request_data: {"session_label": "08-03-26-D51-CPU-T-48-872587"}`). This is also the session's start.
- 1:13 PM–3:05 PM — 13 interventions recorded over the session (rolling `intervention_count` field, roughly one every 8-9 minutes). No individual log line describes what any single intervention was — this comes from a periodic stats snapshot, not a per-event message — so they're reported as a count rather than invented as 13 distinct descriptions.
- 3:05 PM — Session ended (`Ending container: container label is 08-03-26-D51-CPU-T-48-872587.`).

## Not checked
- Rosbag start/stop and production version: no matching log lines found in this export.
- Out-of-moves events: `app:vision`/`app:action` — the categories that would carry `mode.out_of_moves` — aren't present in this export at all. "None found" means "none in what's available," not a confirmed zero for the whole session.

## Summary
447 packages succeeded and 45 failed (19 dropped, 1 dropped on conveyor, 26 grasp failures) over roughly 1 hour 51 minutes. 13 interventions were needed, but the available logs don't show what triggered each one individually. No e-stops or UI-driven changes turned up beyond the operator setting the session label at the start — though that's partly a coverage gap in this export, not a confirmed absence.
```

This demonstrates: reporting a real gap as "not found" rather than guessing (ground rule 1, 2); refusing to invent 13 individual descriptions for the interventions even though ground rule normally calls for narrating anomalies individually — when the only evidence is a counter with no distinguishing detail, a count is the honest option, not a fabrication; and being explicit that "none found" can mean "not covered by this data" rather than "confirmed zero."
</example>

<example illustrative="true">
Illustrative only — demonstrates out-of-moves formatting, not a real finding.

```
**Out-of-moves events:** 2

## Timeline
- 9:47 AM — Out-of-moves event (`mode.out_of_moves`, host:gen1-prod22).
  - Package specs enabled at the time: 14
- 11:02 AM — Out-of-moves event (`mode.out_of_moves`, host:gen1-prod22).
  - Package specs enabled at the time: 0
```

This demonstrates: counting every occurrence into the header line; listing
each one individually rather than rolled into a count, per the out-of-moves
reporting rules; and reporting `0` plainly when no package-specs entry
exists before an occurrence, rather than omitting the line.
</example>

<example illustrative="true">
Illustrative only — demonstrates e-stop root-cause tracing, not a real finding.

```
- 2:15 PM — E-stop (`ESTOP detected with reason`). Root cause traced by
  reading backward ~18 lines to `[USER EXCEPTION] [-10] Arm is not enabled`,
  reason code 12 ("Safety gate opened during active cycle" per the Estop
  Reason Codes reference).
```

This demonstrates: using the primary search, then reading backward from the
secondary line to find the actual cause, then resolving the reason code
against the reference — rather than reporting the e-stop with no root cause.
</example>

<example illustrative="true">
Illustrative only — demonstrates the ground rule 9 fallback for a question
outside the keyword reference, not a real finding.

User asks: "Did the camera ever lose calibration during this session?" — no
keyword reference row covers this.

```
Finding (not keyword-backed — my own read of the logs; treat this as a lead,
not a confirmed finding): searching broadly for calibration-related terms
turned up two lines mentioning "recalibrating extrinsics" from `app:camera`,
about two minutes apart around 10:42 AM. This might indicate a brief
calibration issue, but there's no confirmed keyword for this category yet,
so don't treat this with the same confidence as the keyword-backed findings
above.
```

This demonstrates: exploring instead of refusing when nothing in the
keyword reference matches; still quoting a real line rather than inventing
one; and flagging the finding's lower confidence explicitly rather than
presenting it alongside confirmed findings unmarked.
</example>

</examples>

## Search syntax (Mezmo query language)

This is Mezmo's actual filter syntax — **it applies to the `mezmo_*` tools
(live/hosted log search).** The `logs_*` tools (files uploaded through the
web UI) are a simple substring-search server, not Mezmo — they do **not**
understand any of the syntax below. If asked to filter an uploaded file by
field or level, fall back to a plain substring search and say so, rather
than passing Mezmo query syntax to `logs_search_logs` and assuming it did
something smarter than a literal string match.

**Text filters** — a bare word searches all fields: `searchword`. A phrase
with spaces needs quotes: `"search for this pattern"`. Without quotes,
`my message` means `"my" AND "message"` (two separate word matches, any
order) — not the literal phrase "my message".

**Field filters** — behavior depends on the field's data type:
- `field:*` — field exists at all, e.g. `path:*`.
- **string** fields use *prefix* search: `source:ChooseActiveParcel` matches
  anything starting with that text. No `==` on string fields.
- **number** fields use comparisons: `=`, `<`, `>`, `<=`, `>=` — e.g.
  `total_time:>0.5`.
- **boolean** fields use `==` — e.g. `safe:==true`.

**Combining filters** — a space between filters defaults to `AND`.
`source:ChooseActiveParcel OR source:FlangePathChecker` joins two result
sets. Group with parentheses: `filter1 AND (filter2 OR filter3)`. Negate
with a leading `-`: `-filter1 AND filter2` excludes filter1's matches.

## Log message fields

Mezmo log messages are JSON. Known fields:
- `host` — the machine that logged the message (e.g. `wc3`, `eggplant`).
- `app` — per Mezmo's docs, "the file the log message was written to"
  (e.g. `fastloop.out.log`, `app.err.log`). **Open question:** it's not
  confirmed whether this is a literal filename, a transform of a container
  name (see the container list below), or something else — don't assume a
  mapping (see ground rule 5).
- `level` — e.g. `DEBUG`, `INFO`, `WARNING`. An `ERROR` level has not been
  confirmed to exist — verify before filtering on `level:==ERROR` or
  similar and assuming it will match anything.
- `message` — the display text for the log line.
- `source` — the name of the specific logger/class that wrote the message
  (e.g. `ChooseActiveParcel`, `FlangePathChecker`) — more granular than
  `app` or a container name.
- Additional fields (timestamps, summaries, parcel data) may be present per
  message; expand a line in Mezmo's UI ("Copy line context as JSON") to see
  them for a specific case.

## Dill app containers (glossary — what runs on the robot)

Use this to understand what a log line's origin means when explaining it in
plain English — not confirmed to map directly onto `app:` filter values
(see above).

| Container | What it does |
|---|---|
| `dill_app_camera` | Takes RGB + depth images (Zed 2i / Realsense d455); outputs dimensionalized packages and the point cloud |
| `dill_app_taskloop` | Runs the behavior tree selecting robot control policies sent to the fastloop, ~10 Hz |
| `dill_app_fastloop` | Runs the fastloop control loop — state observations and command processing, ~83.3 Hz (KUKA 12ms period) |
| `dill_app_sound` | (Planned) plays desktop sounds to alert users of errors |
| `dill_app_api-server` | Passes messages/state to the frontend — **likely** where UI-driven operator changes (spec/mode changes) are logged; exact filter value not yet confirmed |
| `dill_app_microphone` | Deprecated. Listened for blower noise profile changes to detect grasping failure |
| `dill_app_package_filtering` | GPU-accelerated filtering for the next package to pick (`ParcelTopmostFilter`, `ParcelCoveredFilter`, `ValidVolumeFilter`, `ParcelQualityFilter`, `PathPlanningFilter`) |
| `dill_app_router` | Parses label-scanner data: pixel location + barcode in, pixel location + carrier + parsed info out |
| `dill_app_rsi_proxy` | Buffers between the rest of the system and the KUKA arm, which requires messages at a specific rate |
| `dill_app_mobile_base` | Processes joystick commands and drive-forward requests; keeps the robot centered via LIDAR in autonomous mode |
| `dill_app_joystick` | External joystick driving control (PS5 controller) |
| `dill_app_zmq_proxy` | All ZMQ pub/sub traffic passes through this — e.g. taskloop → mobile base messages |

**Other containers** (mostly log processing / cloud upload, but expected to exist):

| Container | What it does |
|---|---|
| `pickle_rosbridge` | Runs diagnostics (Foxglove/ROS visualization) and mobile-base LIDAR drivers; connects fastloop and taskloop |
| `docuum` | Prevents stale Docker images from filling robot disk; configurable via `DOCUUM_THRESHOLD` |
| `pickle_datadog` | Logging agent connecting to DataDog |
| `pickle_logdna` | Logging agent connecting to Mezmo (formerly LogDNA) |
| `dill_devcontainer-dill-1` | Dev container, non-production systems only |

## What counts as a "UI / operator change" (explicitly requested — always check for this)

Operator actions taken through the UI (changing the package spec, switching
modes, etc.) most likely surface through `dill_app_api-server` ("passes
messages/state to the frontend" — see glossary above), with phrasing like
"Received request to..." or "Setting ... to" near a timestamp. **This is a
lead, not a confirmed filter value** — the exact `app:`/`source:` value and
phrasing for this service still isn't confirmed (see the open question
under "Log message fields"). Until it is, if you find anything that looks
like an operator-driven change via any tool, call it out explicitly in the
timeline with the log line quoted, and flag in your summary that the exact
search keyword for this category still needs to be pinned down.

## Log keyword reference

### Session time & system status

| What | Keyword(s) to search | Notes |
|---|---|---|
| Login/Logout times | TODO — not yet defined (see "Scope" above; `Received login request` was tried and does not work) | |
| Intervention time | TODO — not yet defined | |
| Rosbag started | `Starting recording to` OR `Recording to` (scope: `host:gen1-prod1`) OR `start writing to bag file` (scope: `app:pickle_rosbridge`) | Confirm rosbag existence/timing |
| Rosbag stopped | `stopped recording` | Confirms when recording ended |

### Package, picking, and placing

| Issue | Keyword(s) to search | Notes |
|---|---|---|
| Box drops | `DROPPED_ON_CONVEYOR` OR `PackageStatus.FAILED_PLACE [ExceptionEnum.DROPPED]` | Confirms a package was dropped after a pick |
| Failed picks | TODO — not yet defined | Look at logs immediately preceding a drop or mode change in the meantime |
| Durable mode | `app:action Current action is pick in mode durable` | No quotes — also catches `very_durable_pick` etc. |
| Conveyor blocked (system mode) | TODO — not yet defined | Indicates the robot entered Conveyor Block recovery mode |
| Conveyor blocked (pick abort) | `PackageStatus.ABORTED_PICK [ExceptionEnum.CONVEYOR_BLOCKED]` | Confirms a pick was aborted specifically due to a blocked conveyor |

### E-stops and safety events

E-stops are critical — always call these out in the timeline and summary.

1. Primary search: `ESTOP detected with reason`
2. If that doesn't give the root cause, secondary search: `[USER EXCEPTION] [-10] Arm is not enabled` — the actual reason is typically 10-30 log lines *above* this line, so read backward from it.
3. Look up the reason code against the Estop Reason Codes reference (ask the user for it if not already provided in this conversation — it isn't embedded in this skill).

### Configuration & system status

| Metric | Keyword(s) to search | Notes |
|---|---|---|
| Prod version & session start times | `app:monitor "Running Dill in production mode on: "` | |
| World config | `loaded world config` | |
| Default config | `using default workflow` | |
| Package specs | `app:vision active list of package_specs` OR `app:vision package specs` | |

### Data and bandwidth

| Metric | Keyword(s) to search | Notes |
|---|---|---|
| Bandwidth mode (on-robot) | `cat ~/env/.report-uploader.env` | Run on-robot, not a log search |
| Bandwidth mode (Mezmo, Dill ≤3.3) | `user@1000.service [offload-bags] Running Rosbag Report (mode: <standard/full/low>)` | |
| Bandwidth mode (Mezmo, after Dill 3.4) | `central-offloader "Processing queued bags"` | |
| Individual bag flow | `app:user "Copying file" bag` | |
| Docker image / docuum limits | `app:docuum "Docker images are using"` | Check image usage vs. configured limit |

### Motion planning (path planning + navigation)

| Issue | Keyword(s) to search | Notes |
|---|---|---|
| Out-of-moves (OOMV) | `mode.out_of_moves` | e.g. `host:gen1-prod17 mode.out_of_moves`. Indicates the robot exhausted planning attempts. **See "Out-of-moves reporting" above — count + package-spec lookup required every time, not just when asked.** |
| Drive failed | `mode.drive_failed` | Indicates an issue with base motion |
| Planner status | `Planning attempt` OR `plannerstatus` | Use to monitor the planning process generally |
| Presence detected | TODO — not yet defined | A presence-detected error throws an internal ESTOP — also search `estop` for this |
| Drive fail (timeout) | `taskloop WARNING Timed out while waiting for action plan` | |

### Localization

TODO — Mezmo error keywords for localization issues not yet defined.
