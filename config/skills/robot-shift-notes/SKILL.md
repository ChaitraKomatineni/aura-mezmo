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

## Scope — ask before searching a whole day of logs

"Shift notes" implies a bounded working period (typically a few hours), not
automatically the entire contents of whatever log file was provided. If the
user hasn't given a specific time window and the available logs span more
than ~4 hours, ask which window they actually want (a shift, an incident
window, "the whole day") before running an exhaustive sweep — an
undifferentiated full-day sweep is slow, tends to exhaust the tool-call
budget, and produces a worse note than a scoped one. If they do want the
whole day, follow the grouping rules below rather than listing every event.

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
   keyword, not separate searches — e.g. `host:==gen1-prod17
   mode.out_of_moves` means "search for `mode.out_of_moves` scoped to host
   gen1-prod17".
5. **If a search returns a very large result, don't try to hold it all in
   context at once.** Use whatever exploration tools are available (grep,
   slice, head-style tools) to narrow it down, or issue a tighter follow-up
   search (add a time window or a more specific term) rather than reading
   the entire raw output.
6. **If you run out of tool-call budget before finishing the full sweep**,
   stop and write the note anyway with what you found, and say explicitly
   which categories you didn't get to check — never fail silently with no
   output at all. A partial, honestly-labeled note is more useful than none.

## Grouping vs. narrating — this is what makes it a "shift note" and not a log dump

Don't give every matching log line its own timeline entry, especially over
a long window. Instead:

- **Narrate individually**: e-stops, box drops, aborted picks, interventions,
  operator/UI-driven changes (spec/mode changes), and anything that isn't
  the robot's normal operating pattern. These are why someone reads a shift
  note.
- **Roll up into a count**: routine successful cycles, ordinary planner
  activity, and repeated non-error status messages. Report these as a
  single line — e.g. "142 successful pick cycles between 06:00 and 14:00,
  averaging ~28s each" — not one bullet per cycle.
- If the same anomaly repeats many times (e.g. 30 conveyor-blocked aborts),
  report it as one entry with a count and the time range, not 30 entries.

## Output format

```
# Shift Notes — <host/robot id if known> — <date/window covered>

**Session:** started <HH:MM:SS>, ended <HH:MM:SS or "still active">
**Rosbag recording:** started <HH:MM:SS>, stopped <HH:MM:SS>
**Production version:** <version, if found>

## Timeline
- HH:MM:SS — <notable individual event, in plain English>
- HH:MM:SS–HH:MM:SS — <rolled-up routine activity, with a count>
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
skipped.

## What counts as a "UI / operator change" (explicitly requested — always check for this)

Operator actions taken through the UI (changing the package spec, switching
modes, etc.) are typically logged by whichever backend/API service handles
UI requests — look for that service's `app:` tag and phrasing like "Received
request to..." or "Setting ... to" near a timestamp. **The exact `app:` tag
and phrasing for this service is not yet confirmed — TODO: fill in once
known.** Until then, if you find anything that looks like an operator-driven
change (spec change, mode change, config change) via any tool, call it out
explicitly in the timeline with the log line quoted, and flag in your
summary that the exact search keyword for this category still needs to be
pinned down by the team.

## Log keyword reference

### Session time & system status

| What | Keyword(s) to search | Notes |
|---|---|---|
| Login/Logout times | `Received login request` | |
| Intervention time | TODO — not yet defined | |
| Rosbag started | `Starting recording to` OR `Recording to` (scope: `host:==gen1-prod1`) OR `start writing to bag file` (scope: `app:pickle_rosbridge`) | Confirm rosbag existence/timing |
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
| Out-of-moves (OOMV) | `mode.out_of_moves` | e.g. `host:==gen1-prod17 mode.out_of_moves`. Indicates the robot exhausted planning attempts |
| Drive failed | `mode.drive_failed` | Indicates an issue with base motion |
| Planner status | `Planning attempt` OR `plannerstatus` | Use to monitor the planning process generally |
| Presence detected | TODO — not yet defined | A presence-detected error throws an internal ESTOP — also search `estop` for this |
| Drive fail (timeout) | `taskloop WARNING Timed out while waiting for action plan` | |

### Localization

TODO — Mezmo error keywords for localization issues not yet defined.
