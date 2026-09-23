---
name: log-app-reference
description: Use when choosing which app, source, level or host to query for a Pickle
  Robot investigation — what each app does, which questions it can and cannot answer,
  verified search keywords, and which apps to cross-reference. Reach for this before
  guessing an app name or a keyword.
---

# Pickle Robot log app reference

Which `app`, `source` and `level` to query for a given investigation, what each app
can actually tell you, and which keywords are safe to search.

## Reliability tags

Every keyword and count below carries one of:

- **`[VERIFIED n/day]`** — measured against the live Mezmo account on Fri 2026-09-18,
  fleet-wide (`host:gen1-prod`), with the count shown. Safe to search.
- **`[CONFIRMED]`** — seen verbatim in a log export a human uploaded, but not
  re-measured live. Safe to search; the count is unknown.
- **`[UNVERIFIED]`** — documented in Confluence or a Jira ticket only, and either
  returns zero live or has not been checked. **Treat as a hypothesis.** If it matters,
  say it is unverified rather than reporting "no results" as if it were a finding.

Do not present an `[UNVERIFIED]` miss as evidence that something did not happen.

## What is already filtered — do not re-apply it

Your queries are scoped by the proxy before they reach Mezmo. **Call
`mezmo_describe_scope` for the live rules.** Do not copy the filter clauses into your
own queries, and do not restate them from this document — this file is about *what the
apps contain*, not about what is filtered.

Two consequences you cannot work around, because the proxy is a hard boundary and you
have no way to relax it:

- **DEBUG is unavailable for every app.** For several apps that is most of their
  volume, so a thin result is often the filter, not the robot. Say so.
- **Some apps are removed entirely.** `describe_scope` lists them. For those,
  "I found no errors" is wrong — the correct answer is "that app is outside this view."

## Apps by usefulness

### `safety_interface` — authoritative for arm state
**`[VERIFIED 15,823/day]`** — INFO 14,250, WARN 487, ERROR 234.

The safety state machine, and the ground truth for arm_enabled / estopped /
intervention transitions. Pickle's own intervention-time benchmark counts windows from
this stream.

```
Current safety system state: X, Prev state: Y. Plc state: Z. Estop reasons: [...]   [CONFIRMED]
Safety state change request: mode: "enable_arm" / "enable_drive"                    [CONFIRMED]
Setting field N for LidarName.FRONT_LIDAR / BACK_LIDAR                              [CONFIRMED]
Resetting safety field selection request / Safety field selection successful         [CONFIRMED]
```

**Can tell you:** the exact state transition, the PLC state, and the E-stop reason.
**Cannot tell you:** what the arm was physically doing — it ticks at ~10Hz against
fastloop's ~83Hz, roughly 8x coarser. Cross-reference `fastloop`.

#### E-stop reason codes — ten are live

| Reason | Live volume |
|---|---|
| `ARM_STOPPED` | `[VERIFIED 3,134/day]` |
| `RADAR_ESTOPPED` | `[VERIFIED 116/day]` |
| `FRONT_LIDAR_ESTOPPED` | `[VERIFIED 109/day]` |
| `BACK_LIDAR_ESTOPPED` | `[VERIFIED 47/day]` |
| `ROBOT_BUTTON_ESTOP` | `[VERIFIED 39/day]` |
| `CONVEYOR_TRIPPED` | `[VERIFIED 33/day]` |
| `INVALID_LIDAR_FIELD` | `[VERIFIED 33/day]` |
| `OPERATOR_STATION_BUTTON_ESTOP` | `[VERIFIED 23/day]` |
| `KUKA_PENDANT_BUTTON_ESTOP` | `[VERIFIED 14/day]` |
| `KUKA_STATE_ESTOP`, `BAD_KEY_STATE`, `TELESCOPIC_ESTOPPED`, `BAD_OPERATOR_STATION_STATE` | `[UNVERIFIED]` — zero live; documented but never observed |

### `taskloop` — task orchestration, and the documented E-stop entry point
**`[VERIFIED 161,287/day]`** — INFO 120,830, WARN 39,164, ERROR 59.

Behaviour-tree / task layer. **Not `action_planning`** — Pickle's own jargon doc lists
this as a common confusion.

```
ESTOP detected with reason: <REASON>                  [CONFIRMED — documented primary E-stop search]
PackageStatus.FAILED_PLACE [DROPPED]                  [CONFIRMED]
DROPPED_ON_CONVEYOR                                   [VERIFIED 19,540/day]
No ranger depth received for package <UUID>           [CONFIRMED]
Starting container: container label is <LABEL>        [CONFIRMED — INFO; session start]
Continuing container: container label is <LABEL>      [VERIFIED — INFO; session resumed]
Ending container: container label is <LABEL>          [CONFIRMED — INFO; session end]
```

**Its INFO survives the filters deliberately**: the container start/stop markers are the
only reliable way to find where a session began and ended.

**But a session label retrieves only those markers — never the session itself.**
`session_label` and `session_id` exist as real indexed fields, and they are written
**only** when an operator creates or closes a session. Everything the robot actually
did carries neither. On a measured real session (`CMAU6379806`, gen1-prod2):
`session_label:==CMAU6379806` → **10 lines**; the session's actual activity →
thousands of lines, **none of them labelled**. Fleet-wide only ~42 lines/day carry
the field at all.

So use the label exactly once, to read the start and stop timestamps, then **drop it
and query by `host` + time window.** If you leave the label in the query you will get
a handful of lines and may wrongly report that the session was empty — a false
negative about a physical machine. A near-empty label result means the query was
wrong, not that the robot was idle.

Two related traps: a session can run 20+ hours and cross midnight (the `CMAU6379806`
session ran 14:28Z → 12:38Z the next day), so a start and end on different dates is
normal. And free-text searching the label instead of the field matches the container
number wherever it is embedded in JSON payloads — mostly DEBUG lines that the filters
drop — so it looks like a hit count without being usable evidence.

Note `DROPPED_ON_CONVEYOR` `[VERIFIED 19,540/day]` and fastloop's
`PACKAGE_DROPPED_ON_CONVEYOR` `[VERIFIED 1,900/day]` are **different strings with
different volumes** — do not treat them as interchangeable.

### `fastloop` — controller state and RSI timing
**`[VERIFIED 6,041/day surviving]`** — WARN 5,114, ERROR 107, unleveled 820.

The real-time KUKA control loop over RSI, ~83–250Hz. `source` values are either
infrastructure (`KUKAInterface`, `ControllerManager`, `FastLoop`) or motion controllers
(`BlendedMotion`, `Vacuum`, `BounceMotions`, `BounceDwell`, `UndoBounce`,
`ReturnAndMeasure`, `ReturnObserver`, `PlaceHeightCorrection`, `ScanningMotion`,
`WaitForConveyorClear`, `WaitUntilPackageDetached`, `WaitForArmStop`, `Combined`) or
kinematics helpers (`PackageDepthEstimator`, `WorkspaceManager`, `MaxJointSpeedFinder`,
`LINMotionSpeedChecker`).

**fastloop IS worth querying.** An earlier version of this reference claimed it emitted
nothing above DEBUG and was therefore useless under the filters. That was wrong:
5,221 WARN/ERROR lines survive per day. Query it for arm incidents.

What is lost: DEBUG (63.3M/day) and INFO (11.3M/day) are both excluded, so per-cycle
controller tracing and RSI delay timing are **not** available. The surviving lines are
the problems, not the narrative.

```
Cleaning up controller Combined[ReturnAndMeasure]: PACKAGE_DROPPED_ON_CONVEYOR  [CONFIRMED — failure]
Cleaning up controller Combined[BounceDwell]: BOUNCE_SUCCESS_TIMEOUT            [CONFIRMED — failure]
Stop controller with status INTERRUPTED                                         [CONFIRMED — DEBUG, not visible]
[USER EXCEPTION] Arm is not enabled                                            [VERIFIED 732/day for "USER EXCEPTION"]
```

`INTERRUPTED` has four possible causes — an internal stop check, a speed-limit check,
the arm losing enable, or fastloop shutting down — and **does not by itself tell you
which**. It is also DEBUG, so you will not see it; use `safety_interface` instead.

**Cannot tell you:** any real sensor value. The `Vacuum` controller logs a success/fail
status code only, never a pressure reading — use `system_health`.

### `motor_controller` — hardware fault layer
**`[VERIFIED 9,008/day]`** — WARN 4,962, INFO 1,546, ERROR 130.

```
Controller is not ok. ... Abort message received: 0x581ff01   [CONFIRMED]
STO is engaged                                                [VERIFIED 723/day]
```

**Can tell you:** servo-level abort codes and whether **Safe Torque Off** was physically
engaged — the electrical signal, distinct from a `safety_interface` state change though
usually co-occurring. Low volume, high signal.

### `vision` — perception and per-package confidence
**`[VERIFIED 1,261,998/day]`** — INFO 846,299, WARN 415,224, ERROR 46.

`PackageGraspDetector` pipeline: segmentation → surface matching → package construction
→ filtering.

```
Dimension estimation failed. No dim_estimate_candidates.            [CONFIRMED]
Failed to classify the package as any of the N provided package specs.  [CONFIRMED]
Corner points too close. dist=N                                     [CONFIRMED]
Face corners or seen dims are None, skipping occlusion calculation   [CONFIRMED]
No faces found / Face WorldDir.FRONT has no seen dims               [CONFIRMED]
active list of package_specs                                        [VERIFIED 52/day]
package specs                                                       [VERIFIED 23,215/day]
```

Structured quality report (`SinglePackageConstructor`): `package_quality` with
`observability: {depth, major, minor}`, `seen_fit_pct_errors`, and `surfaces_quality`
with `xyzs_plane_rmse`, `iou_face_penalty`.

**Can tell you:** the only per-package confidence metrics anywhere — which dimensions
were observed vs estimated, and how well a face's plane fit the point cloud.

Its **415,224 WARN lines/day** are worth treating as a standing condition to explain,
not background.

### `scan_perception` — highest error volume in the fleet
**`[VERIFIED 366,045/day]`** — WARN 295,716, **ERROR 68,968**, INFO 634.

Not characterised in detail yet, but note the scale: ~69,000 errors a day is the largest
error source that survives filtering. If an investigation finds nothing elsewhere, look
here. Treat a high count as a chronic condition rather than a per-incident signal.

### `path_planning` — trajectory generation
**`[VERIFIED 1,143,861/day surviving]`** — unleveled 1,060,363, WARN 83,400, ERROR 98.

Three stages: feasible planning → refinement/optimisation → speed setting.

**~93% of surviving lines are unleveled fragments of a chunked JSON trajectory /
point-cloud dump** — a single message exceeding the line-size limit, split across dozens
of lines of bare coordinate arrays. They cluster as near-unique templates and are
meaningless individually.

But not all of them: the same unleveled block carries real outcomes, e.g.
`{"message":"Pick planning complete for <uuid>. Planning SUCCEEDED"` `[CONFIRMED]`.
So filter to lines with a non-null `message` rather than discarding unleveled lines.

```
Unsafe edge for package group <UUID>: PointCloudViolation / WorkspaceViolation /
    ObstacleViolation / SelfCollisionViolation / FailedDiscretizationDetails   [CONFIRMED]
optimized path for package <UUID> is unsafe, returning original               [CONFIRMED]
No kinematically feasible measurement poses for package group <UUID>          [CONFIRMED]
Cannot attach intermediate pose / discretized steps are not safe              [CONFIRMED]
```

**Read `optimized path is unsafe, returning original` as a graceful fallback, not a
failure** — the architecture deliberately continues with the earlier feasible path. A
burst of `Unsafe edge` rejections often correlates with an active safety-field
violation elsewhere; check `safety_interface` for the same window.

### `action_planning` — high-level pick/drive decisions
**`[VERIFIED 44,910/day surviving]`** — WARN 39,468, unleveled 5,370, ERROR 72.

Decides whether to pick or drive, groups and filters candidate packages, feeds
`path_planning`. **Not `taskloop`.**

```
Plan Modification Failed for <ID>: No Safe Paths for Valid Candidates.   (SafePivotPlanModifier)  [CONFIRMED]
Plan Modification Failed for <ID>: No Pivot in Approach.                 (SafePivotPlanModifier)  [CONFIRMED]
Expected at least 4 face contour points, got N.                          (PackageQualityFilter)   [CONFIRMED]
MaxDimFilter Failed for group <UUID>. Max dim failed                     (MaxDimFilter)           [CONFIRMED]
Collision free sampling volume could not be constructed.                 (FreeSpacePoseSampler)   [CONFIRMED]
Pre-measurement to measurement infeasible                                (MeasurementPlacePlanner)[CONFIRMED]
```

**This app looks nearly silent and that is the filter, not the robot** — 44,910 of
17.9M lines survive, because DEBUG (12.1M) and INFO (5.7M) are both excluded. Never
infer from a thin result that action_planning was idle.

`SafePivotPlanModifier` failures dominate what remains; each logs a unique per-package
hex ID, so normalise the ID before clustering or it looks like hundreds of templates.

### `system_health` — the only real sensor telemetry
**`[VERIFIED 22,288/day]`** — INFO 9,539, WARN 8,574, ERROR 104.

~30-second periodic health checks across circuits, gripper/pneumatics and IPC.

```
System Health Report:                                        [CONFIRMED]
Blower Pressure (psi): -5.26                                 [CONFIRMED]
End Effector Pressure (psi): 0.0                             [CONFIRMED]
Vacuum Valve: light_vacuum / full_vacuum                     [CONFIRMED]
Attached: True / False                                       [CONFIRMED]
```

**The only confirmed source of actual pressure/vacuum values.** fastloop's `Vacuum`
controller carries none. **Limitation:** ~30s sampling, so it cannot be tied to a
specific sub-second pick attempt — use it for trend, not for a single cycle.

### `api-server` — operator actions and UI relay
**`[VERIFIED 2,223/day INFO surviving]`**

Bridge between the operator UI and the ROS apps (SSE out, POST in). Its DEBUG (~19,930
per sample) is excluded, and that DEBUG was the per-item detail — what survives is the
INFO headline events.

```
Login request with operator name X                                    (HandleLoginRoute)          [CONFIRMED]
Updated arm_speed: N / max_pph: N / durability: X                     (ContainerSettingsManager)  [CONFIRMED]
Request to update and save package specs received:                    (SetPackageSpecsRoute)      [CONFIRMED]
Commanding mobile base to move Nm for drive with request uuid: <UUID> (PublishMobileBaseCommandRoute) [CONFIRMED]
```

**Can tell you:** what the operator saw and did — logins, settings changes, drive
commands, spec submissions. **Not a primary data source**; it relays what other apps
computed. Note it shows specs being *submitted*, not which are currently *active* — for
that use `vision`'s `active list of package_specs`.

### `dill-user` — operator bug reports ONLY. Never a root cause.
**`[VERIFIED 1,207 lines / 30 days]`** — 598 at FATAL, 609 the same payload unleveled.

Every line is a human pressing the bug-report button in the UI:

```
(User Submitted Bug Report) site: <site>, dill_version: <ver>, robot: <host>,
operator: <name>, description: <what they typed>
```

**There is no robot behaviour in this app.** It is visible to you on purpose — "show
me the bug reports for this robot yesterday" is a real question and this is where the
answer lives — but it is never evidence of a cause.

**The trap:** it is logged at FATAL, so a top-down severity walk finds it first and it
reads like a fault ("wont retract arm"). On ticket #8055 it was the only production
FATAL in the window, and calling it the critical event produced a confident wrong
answer. Note it as the report, then keep descending.

**Use it as an anchor, not an answer:**

| field | what it gives you |
|---|---|
| its timestamp | an **upper bound** — the fault is earlier. Search backwards. |
| `most_recent_bag` | the **mode name**, not a better timestamp (measured: only 4–213s, median 33s, before the report). `N/A` in ~15% of reports. Not in the Freshdesk ticket. |
| `operator` | who to ask |
| `description` | the operator's wording — for checking a candidate at the end, never for searching |

**Bag mode vocabulary** `[VERIFIED from a 41-line FATAL export]` — the robot's own
label for its state, independent of what the operator typed:

```
out_of_moves_mode            container finished, nothing left to pick
localization_mode            re-localising
localization_failure_mode    localisation gave up
drive_failed_mode            drive fault
recover_failed_mode          a recovery attempt had already failed
maintenance_mode             parked for maintenance
auto_driving_mode            driving autonomously
commanded_driving_mode       driving under operator command
picking_<n>                  pick cycle n, still in progress
picking_<n>_placed           pick n completed and placed
picking_<n>_unknown          pick n ended in an unknown state
picking_<n>_aborted_pick     pick n aborted
```

A trailing **`.bag.active`** means the file was still being written — the robot was
STILL IN that mode when the button was pressed. A plain `.bag` means that mode had
already ended.

**Use the mode to corroborate or contradict the description.** `DRIVE FAILED` with
`drive_failed_mode` agrees. `BOXES WEDGED` with `picking_8_placed` does not — that
pick completed and placed, so the wedge came *after* a success. Both readings are
useful; say which one you have.

**Do not assume a bug report means something broke.** 59% of a measured sample were
routine or administrative: `PRESENCE DETECTED` (someone entered the cell) ×11,
`NO MORE PICKS` ×9, plus `CONVEYOR REPAIR`, `POWER CYCLING`, a container-number
question, and a request for more filters.

**The app name is not a reliable identifier.** In that sample 40 lines carried
`app:dill-user` and one carried a `docker-<hash>.scope` app name with an identical
payload. Identify these by shape instead: FATAL, with `description` + `operator` +
`robot`.

See "A report of a fault is not the fault" in the system prompt; it applies to every
question, not just ticket RCA.

### Lower volume, less characterised
`navigation` `[VERIFIED 26,089/day]` · `camera` `[VERIFIED 18,888/day, ERROR 444]` ·
`safety_interface` see above · `monitor` `[VERIFIED 7,978/day]` ·
`workspace` `[VERIFIED 7,794/day]` · `pendant` `[VERIFIED 7,194/day, ERROR 1,840]` ·
`containerd.service` / `docker.service` / `logdna-agent.service` / `init.scope` —
container and logging infrastructure, not robot behaviour.

### Apps that do not exist on production
`cartographer_node`, `foxglove_bridge`, and `rosbag` / `dill_rosbag` /
`rosbag_connector` all return **zero** lines. They are documented elsewhere but are not
shipping from `gen1-prod*`. Do not report their absence as a finding, and do not spend
a query on them.

## Search terms to avoid

| Term | Why |
|---|---|
| `[-10]` | `[VERIFIED 97,199,166/day]` — punctuation is stripped during tokenisation, so this matches almost everything. Useless as a filter. |
| `"Robot Initiated Stop"` | `[VERIFIED 10,908,163/day]` — routine fastloop INFO, not a rare event, and excluded from this view anyway. |
| A literal `AND` keyword | Whitespace already means AND. A literal `AND` produced an identical count in testing, so it appears tolerated, but it is not documented — use whitespace. |

## Query syntax that matters here

```
app:task            partial / prefix match — app:task also matches taskloop
host:gen1-prod2     the proxy rewrites this to an exact match for you; just write it normally
field:==value       force an exact match — needed on app:, source:, session_label:
(a OR b)            explicit OR, spaces inside the parens
a b                 whitespace is AND
-"exact phrase"     exclude
field:*             field exists — the ONLY valid use of *
```

Wildcards are **not** supported in values. `app:fastloop*` is invalid; prefix matching
is automatic. The proxy only exact-matches `host:` — on every other string field you
must write `==` yourself, or a name that is a prefix of a sibling's silently sweeps the
sibling in (`session_label:CMAU` matched 22 lines across several containers where
`session_label:==CMAU6379806` matched 10).

**Never scope an investigation by `session_label` or `session_id`.** They are written
only when an operator opens or closes a session, so they identify *boundaries*, not
contents — see `taskloop` above. Read the boundary timestamps, then query by `host` and
time window.

## Recipes

**Reconstructing a session from its label** (do this before any other recipe when the
user names a session/container)
1. `app:taskloop level:info "Starting container"` + the label → read the timestamp.
2. `app:taskloop level:info "Ending container"` + the label → read the timestamp. If
   there is no end yet, the session is still open; if the end is on the next calendar
   day, that is normal.
3. **Discard the label.** Every subsequent query is `host:==<robot>` with
   `from_time`/`to_time` set to that window and nothing else carried over.
4. Only now pick the recipe that matches the question.

Step 3 is the one that gets skipped. Carrying the label forward returns almost nothing,
because the work of a session is not labelled.

**Arm / safety incident (E-stop, intervention, unexpected stop)**
1. `safety_interface` — the state transition and reason code (ground truth)
2. `taskloop` — `ESTOP detected with reason`, the documented entry point
3. `motor_controller` — STO / abort codes, hardware confirmation
4. `fastloop` — its surviving WARN/ERROR; controller cleanup failures
5. `dill-user` — whether an operator reported it, and `most_recent_bag` for the
   robot's own label for the state it was in. Read it for the anchor, never as
   the cause (see above).

Remember `GRIPPER_BREAKAWAY` is the most common reason by two orders of magnitude, so
finding one is not itself remarkable.

**Grasp / pick quality**
1. `vision` — per-package confidence, dimension and classification failures
2. `action_planning` — filtering and plan-modification failures (pre-planning)
3. `path_planning` — `Unsafe edge` rejections and pick outcomes (non-null message only)

**Vacuum / pressure**
`system_health` only. Nothing else carries a pressure value. ~30s resolution.

**Operator actions / configuration audit**
`api-server` INFO for what was submitted; `vision`'s `active list of package_specs` for
what is actually active.

**Package specs at a moment in time**
`vision` `active list of package_specs` `[VERIFIED 52/day]` — low volume, so widen the
window rather than concluding it is absent.
