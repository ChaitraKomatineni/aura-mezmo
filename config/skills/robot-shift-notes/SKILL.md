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
count and per-event package-spec numbers.

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
