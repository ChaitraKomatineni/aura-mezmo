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
