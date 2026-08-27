# Drain3 explorer

A standalone experiment, separate from the rest of this repo: feed it a
batch of unstructured logs and see what templates
[Drain3](https://github.com/logpai/Drain3) mines out of them. Nothing here
is wired into `docker-compose.yml`, the Aura agent, or the web UI — it's a
sandbox for evaluating whether template mining is worth building into the
pipeline (e.g. as a new `logs-mcp` tool, or to auto-populate the
`robot-shift-notes` skill's keyword table) before committing to that.

## What it does

Drain3 reads log lines one at a time and clusters them into templates —
e.g. `Starting container: sess-A912` and `Starting container: sess-B004`
both collapse into one template, `Starting container: <LABEL>`. Run it
over a real log file and you get back: every distinct template it found,
how many times each occurred, which `app`/`level` it showed up under, and
a couple of example raw lines per template.

## Setup

```bash
cd experiments/drain3-explorer
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
```

## Try it on the smoke-test fixture first

`sample_logs/smoke_test.log` is a tiny synthetic file (borrowed from
Drain3's own docs) just to confirm the tool runs before pointing it at
real data:

```bash
python3 mine_templates.py --input sample_logs/smoke_test.log
```

You should see it collapse 3 "connected to ..." lines into one template,
2 "Hex number ..." lines into another, 3 "user ... logged in" lines into a
third, and so on.

## Run it on real logs

Point `--input` at any log file, directory, or glob — either a plain-text
log or a Mezmo/LogDNA `.jsonl` export (one JSON object per line, like the
files `logs-mcp` already serves). For JSON-line input it pulls the
`message` field by default (`--field` to change that) and also tracks
`app`/`level` per template for the summary:

```bash
python3 mine_templates.py --input /path/to/your-export.jsonl --top 40

# write the full result set (not just the top N printed to screen) to a file
# -- each entry includes cluster_id, count, template, apps, levels,
# first_seen/last_seen (ISO 8601, or null if no line in that cluster had a
# derivable timestamp), and example raw lines
python3 mine_templates.py --input /path/to/your-export.jsonl --output results.json

# multiple inputs, or a whole directory
python3 mine_templates.py --input day1.jsonl --input day2.jsonl
python3 mine_templates.py --input /path/to/logs_dir/

# quick sanity check on a huge file without processing all of it
python3 mine_templates.py --input huge-export.jsonl --limit 5000
```

`--persist state.bin` saves Drain3's learned clusters to a file and reloads
them on the next run, so knowledge accumulates across multiple log batches
instead of starting from zero each time (see Drain3's persistence feature).

## Reducing what you feed the LLM (`--collapsed-output`)

Drain3 itself is content-only — `LogCluster` has no timestamp field, just
tokens, an id, and a count. Timestamps matter a lot for this project (shift
notes need "operator logged out between X and Y"), so this script tracks
first/last-seen per cluster itself, outside of Drain3: it pulls whichever
timestamp field a line actually has (`timestamp_iso`, then `timestamp`, then
Mezmo's own ingestion clock `_ts` as a fallback that's present on
essentially every line regardless of source app — confirmed 100% coverage,
20,000/20,000 lines, on the full p16 export). `--collapsed-output` writes
that out as one line per template, sorted chronologically, instead of the
full result set:

```bash
python3 mine_templates.py --input /path/to/your-export.jsonl --collapsed-output collapsed.txt
```

```
[2026-08-03T19:08:40Z -> 2026-08-03T19:09:27Z] x3934 (fastloop) [DEBUG]  Stop controller with status STOPPED
[2026-08-03T19:08:45Z -> 2026-08-03T19:09:25Z] x84 (audit) [-]  AVC apparmor "DENIED" operation "ptrace" ...
[2026-08-03T19:09:26Z] x1 (user@1000.service) [ERROR]  [SINGLE OCCURRENCE]  [temperature-probe] ERROR [Errno <NUM>] No such file or directory ...
```

`(app)` and `[level]` are both tracked and shown per cluster. `[-]` for level is
an honest "this source doesn't report one" -- confirmed on the real p16
export: `audit`/`kernel`/`tailscaled.service`/`central-offloader` never carry
a `level` field at all, while `fastloop`/`pickle_rosbridge`/`user@1000.service`
do, including the one real `[ERROR]` in this dataset (the temperature-probe
fault) -- exactly the kind of thing worth a severity filter or a "surface all
ERRORs regardless of count" rule downstream.

On the real p16 exports this ran on: the single-session file collapsed 475
raw lines into 5 (95x), and the full 20k-line firehose collapsed into 51
(392x). `[SINGLE OCCURRENCE]` flags count-1 clusters specifically because
those are the ones worth narrating individually in a shift note, per
`robot-shift-notes`' own "group routine activity, narrate anomalies" rule —
this output is close to the raw material that rule already asks for.

One honest limit: this is currently a batch tool, not a running service —
it discovers first/last-seen and templates only after seeing the whole
file. Wiring it into `logs-mcp` as a live tool (see below) would need
streaming updates instead of a single upfront pass.

### Low-count clusters show every occurrence, not just first/last

Collapsing to first/last-seen is fine for a cluster that fired 3,934 times —
nobody needs all 3,934 timestamps. It's a real information loss for a
cluster that fired 3 times, where "first -> last" silently erases the
middle one and implies an even, unremarkable spread that might not be true.
Real example from the p16 export: a `"Node (...) differs by translation..."`
template with 3 occurrences turned out to be `19:08:50Z, 19:09:09Z,
19:09:09Z` -- one early hit, then two simultaneous ones 19 seconds later --
which a `19:08:50Z -> 19:09:09Z` range would have made look like a smooth,
unremarkable gap instead of a burst pattern.

So `--full-timestamps-below N` (default 10) keeps *every* occurrence's
timestamp for any cluster at or below that count, in both `--output` and
`--collapsed-output`; only clusters above it collapse to a first/last
range. Both `--output`'s `all_timestamps` field and `--collapsed-output`'s
bracketed list use this — a cluster over the threshold gets `null` /
`first -> last` rather than a silently truncated partial list.

For clusters that *do* stay collapsed to a range (the genuinely
high-volume ones), the `examples` field this tool already writes is the
bridge back to precision when it's actually needed: it's real, literal
text from that template, which Aura can hand straight to `logs_search_logs`
to pull every exact occurrence with full timestamps on demand. That search
is cheap specifically *because* these clusters are high-count in the raw
firehose but the ones actually worth that kind of drill-down are rare --
so most of the time nothing needs it, and when something does, the lookup
targets one template instead of scanning the whole file.

## Training vs. inference mode (`--mode`)

Everything above runs in Drain3's *training* mode (`add_log_message()`):
every line can create a new cluster or generalize an existing template.
That's the right mode for a one-off exploratory pass, but it means the
template catalog can drift a little between runs and every genuinely novel
line just quietly becomes cluster #200 with no signal that it was novel.

Drain3 also has an [inference mode](https://github.com/logpai/Drain3#training-vs-inference-modes)
(`match()`): given an already-trained catalog, it classifies new lines
against it *without ever creating or modifying a cluster*. A line that
doesn't match anything comes back `None` instead of silently becoming a
new template.

```bash
# train once on a representative batch, save the catalog
python3 mine_templates.py --input historical.jsonl --persist catalog.bin

# classify new logs against ONLY that catalog -- read-only, nothing learned
python3 mine_templates.py --input new_logs.jsonl --persist catalog.bin --mode infer
```

`--mode infer` requires an existing `--persist` file (it errors otherwise --
there's nothing to classify against). Unmatched lines are reported as a
single `(unrecognized -- no match in the trained catalog)` pseudo-cluster
(`cluster_id -1`) rather than being dropped or silently learned, and get
their own `apps`/count/timestamps like any other row.

Tried this for real: trained a catalog on the p16 export (gen1-prod16, 51
templates), then ran it in `--mode infer` against the entirely separate
p22 export (gen1-prod22 -- a different robot). 97.3% of p22's 20,000 lines
(19,455) matched a template the p16 catalog had never seen anything from
that robot to learn -- `fastloop`, `audit`, `kernel`, `temperature-probe`,
`talker`, `system_health` all matched cleanly. The remaining 2.7% (545
lines) came from a mix of apps p16 never had at all (`echoer`,
`miru.service`, `scan_perception`, `ssh.service`) and apps present in both
robots but with message content p16's catalog had never seen (some
`pickle_rosbridge`/`tailscaled.service`/`logdna-agent.service` variants) --
`match()` requires a perfect match (`sim_th=1.0` internally, stricter than
training's `sim_th`), so it won't stretch an existing template to cover a
merely-similar new variant the way training would.

That 545-line breakdown is itself a genuinely useful anomaly signal for a
fleet with more than one robot: "recognized against a shared baseline" vs.
"never seen before, from any robot in training" is a distinction plain
frequency counting doesn't give you.

## Reading the output

```
  COUNT    ID  APPS                  TEMPLATE
-------------------------------------------------------------------
    212     3  taskloop              Package <NUM> <*> succeeded
     45     3  taskloop              Package <NUM> <*> failed: <*>
      8     7  api-server            Ending container: container label is <LABEL>
```

- **COUNT** — how many log lines matched this template. Sort order is
  count descending, so the top of the list is your most routine, highest-
  volume events — good candidates for "group into a count" in a shift-note
  style summary. The bottom of a `--top` run with a high `--top` value is
  where rare, possibly-anomalous one-off messages show up.
- **APPS** — which `app` field(s) this template showed up under, if the
  input was JSON. Useful for confirming a template only ever comes from
  the container/service you expect.
- **TEMPLATE** — the mined template itself, with variable parts replaced
  by `<NAME>` (see `drain3.ini`'s masking config for what each name means
  — `<LABEL>`, `<UUID>`, `<NUM>`, `<IP>`, or Drain's own generic `<*>`
  catch-all for anything not explicitly masked).

## Tuning `drain3.ini`

The masking rules and `sim_th` in `drain3.ini` are a starting point, not a
finished config — read them alongside a real run's output and adjust:

- **Two templates that should be one** (e.g. it treats "Ending container:
  ... 08-03-26-D51-CPU-T-48-872587" and "Ending container: ...
  09-14-26-A22-GPU-T-12-441098" as different templates) usually means a
  variable part isn't being masked yet, so Drain sees literal differences
  as two message shapes instead of a wildcard. Add or adjust a `masking`
  regex to catch it.
- **One template that should be several** (e.g. genuinely different event
  types getting merged) usually means `sim_th` is too low, or too much of
  the line got masked away, leaving too few real tokens to tell messages
  apart. Try raising `sim_th`, or narrowing an over-eager mask.

## Where this could go next (not built yet)

- A `logs_summarize_templates` tool on `logs-mcp`, so a shift-notes request
  starts from a compact template census (counts per event shape) instead
  of Aura discovering repetition itself via Scratchpad's token-threshold
  exploration — genuinely complementary to Scratchpad, not a replacement
  for it.
- Auto-drafting rows for `robot-shift-notes/SKILL.md`'s keyword reference
  table from whatever templates a run surfaces, instead of relying on
  someone noticing and confirming each keyword by hand.
- `extract_parameters()` (see Drain3's docs) to pull structured values —
  session IDs, counts — straight out of matched lines, rather than the
  skill instructing the LLM to eyeball a raw string for them.

None of this is wired up — this folder is deliberately just the
"what does the output look like" step before deciding if/how to build any
of it in.
