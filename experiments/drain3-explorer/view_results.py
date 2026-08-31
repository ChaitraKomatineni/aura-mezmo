#!/usr/bin/env python3
"""
view_results.py — turn a mine_templates.py --output JSON file into a single
self-contained HTML page you can open in a browser: a searchable, sortable,
filterable table (colored severity badges, click-to-expand examples/full
timestamps) instead of scrolling raw JSON or a plain-text dump.

Works with either --output shape mine_templates.py can produce:
  - pooled mode: a JSON list of template entries
  - --by-app mode: a JSON object of {app_name: [template entries]}

Usage:
    python3 mine_templates.py --input logs.jsonl --output results.json
    python3 view_results.py --results results.json
    # -> writes results.html next to it; open it in any browser, no server needed

    python3 view_results.py --results results.json --output report.html
"""
import argparse
import html
import json
from pathlib import Path


def load_rows(results_path):
    """Normalize either --output shape into one flat list of row dicts,
    each carrying a "group" field (the app name in --by-app mode, or
    "all" for pooled mode -- kept so the viewer can still filter/label
    by it even though the two input shapes differ)."""
    data = json.loads(Path(results_path).read_text(encoding="utf-8"))
    rows = []
    if isinstance(data, list):
        for entry in data:
            entry = dict(entry)
            entry["group"] = "all"
            rows.append(entry)
    elif isinstance(data, dict):
        for group, entries in data.items():
            for entry in entries:
                entry = dict(entry)
                entry["group"] = group
                rows.append(entry)
    else:
        raise ValueError("results JSON must be a list or an object of lists")
    return rows


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Drain3 template viewer{title_suffix}</title>
<style>
  :root {{
    --bg: #0d1117; --panel: #161b22; --border: #30363d; --text: #c9d1d9;
    --dim: #8b949e; --accent: #58a6ff; --mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--bg); color: var(--text); margin: 0; padding: 24px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  .subtitle {{ color: var(--dim); font-size: 13px; margin-bottom: 18px; }}
  .stats {{ display: flex; gap: 12px; margin-bottom: 18px; flex-wrap: wrap; }}
  .stat {{
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 14px; min-width: 110px;
  }}
  .stat .num {{ font-size: 20px; font-weight: 600; }}
  .stat .label {{ font-size: 11px; color: var(--dim); text-transform: uppercase; letter-spacing: .04em; }}
  .controls {{ display: flex; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; align-items: center; }}
  input[type=text] {{
    background: var(--panel); border: 1px solid var(--border); color: var(--text);
    padding: 8px 12px; border-radius: 6px; font-size: 13px; width: 280px;
  }}
  select {{
    background: var(--panel); border: 1px solid var(--border); color: var(--text);
    padding: 8px 10px; border-radius: 6px; font-size: 13px;
  }}
  .chip {{
    display: inline-block; padding: 4px 10px; border-radius: 999px; font-size: 12px;
    border: 1px solid var(--border); cursor: pointer; user-select: none; color: var(--dim);
    background: var(--panel);
  }}
  .chip.active {{ color: #fff; border-color: transparent; }}
  .chip[data-level="ERROR"].active {{ background: #d1373b; }}
  .chip[data-level="WARN"].active, .chip[data-level="WARNING"].active {{ background: #b3811f; }}
  .chip[data-level="INFO"].active {{ background: #1f6feb; }}
  .chip[data-level="DEBUG"].active {{ background: #57606a; }}
  .chip[data-level="-"].active {{ background: #57606a; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  thead th {{
    text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border);
    color: var(--dim); font-weight: 500; cursor: pointer; white-space: nowrap;
    position: sticky; top: 0; background: var(--bg);
  }}
  thead th:hover {{ color: var(--text); }}
  tbody tr {{ border-bottom: 1px solid var(--border); cursor: pointer; }}
  tbody tr:hover {{ background: var(--panel); }}
  td {{ padding: 8px 10px; vertical-align: top; }}
  td.count {{ font-weight: 600; text-align: right; white-space: nowrap; }}
  td.template {{ font-family: var(--mono); font-size: 12px; }}
  td.when {{ color: var(--dim); font-size: 11px; white-space: nowrap; font-family: var(--mono); }}
  .badge {{
    display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px;
    font-weight: 600; margin-right: 4px; color: #fff;
  }}
  .badge.ERROR {{ background: #d1373b; }}
  .badge.WARN, .badge.WARNING {{ background: #b3811f; }}
  .badge.INFO {{ background: #1f6feb; }}
  .badge.DEBUG {{ background: #57606a; }}
  .badge.dash {{ background: #57606a; }}
  .badge.single {{ background: #8957e5; }}
  .badge.unrecognized {{ background: #d1373b; }}
  .apps {{ color: var(--dim); font-size: 11px; }}
  .detail {{ background: var(--panel); }}
  .detail td {{ padding: 12px 16px; font-family: var(--mono); font-size: 12px; color: var(--dim); }}
  .detail .ex {{ color: var(--text); margin: 2px 0; white-space: pre-wrap; word-break: break-all; }}
  .detail .ts {{ margin-top: 8px; }}
  .hidden {{ display: none; }}
  .empty {{ padding: 40px; text-align: center; color: var(--dim); }}
</style>
</head>
<body>
  <h1>Drain3 template viewer</h1>
  <div class="subtitle">{subtitle}</div>

  <div class="stats" id="stats"></div>

  <div class="controls">
    <input type="text" id="search" placeholder="Search templates or apps...">
    <select id="groupFilter"></select>
    <div id="levelChips"></div>
  </div>

  <table>
    <thead>
      <tr>
        <th data-key="count">Count &#8595;</th>
        <th data-key="group">Group</th>
        <th data-key="apps">Apps</th>
        <th data-key="levels">Level</th>
        <th data-key="template">Template</th>
        <th data-key="first_seen">When</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="empty hidden" id="emptyMsg">No templates match the current filters.</div>

<script>
const ROWS = {rows_json};

let sortKey = "count", sortDir = -1;
let activeLevels = new Set();
let activeGroup = "";
let searchText = "";

function levelClass(lv) {{
  if (!lv || lv === "-") return "dash";
  const up = lv.toUpperCase();
  if (["ERROR","WARN","WARNING","INFO","DEBUG"].includes(up)) return up;
  return "dash";
}}

function fmtApps(apps) {{
  if (!apps || !apps.length) return "-";
  if (apps.length <= 2) return apps.join(", ");
  return apps.slice(0,2).join(", ") + " +" + (apps.length - 2);
}}

function fmtWhen(row) {{
  if (row.all_timestamps && row.all_timestamps.length) {{
    if (row.all_timestamps.length === 1) return row.all_timestamps[0];
    return row.all_timestamps[0] + " (+" + (row.all_timestamps.length - 1) + " more)";
  }}
  if (row.first_seen && row.last_seen && row.first_seen !== row.last_seen) {{
    return row.first_seen + " &rarr; " + row.last_seen;
  }}
  return row.first_seen || "no timestamp";
}}

function escapeHtml(s) {{
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}}

function buildStats() {{
  const totalLines = ROWS.reduce((a, r) => a + (r.count || 0), 0);
  const groups = new Set(ROWS.map(r => r.group));
  const apps = new Set(ROWS.flatMap(r => r.apps || []));
  const stats = [
    [ROWS.length, "Templates"],
    [totalLines, "Raw Lines"],
    [groups.size > 1 ? groups.size : apps.size, groups.size > 1 ? "App Groups" : "Apps"],
    [ROWS.filter(r => r.count === 1).length, "Single Occurrence"],
  ];
  document.getElementById("stats").innerHTML = stats.map(([n, l]) =>
    `<div class="stat"><div class="num">${{n.toLocaleString()}}</div><div class="label">${{l}}</div></div>`
  ).join("");
}}

function buildControls() {{
  const groups = Array.from(new Set(ROWS.map(r => r.group))).sort();
  const groupSel = document.getElementById("groupFilter");
  if (groups.length <= 1) {{
    groupSel.classList.add("hidden");
  }} else {{
    groupSel.innerHTML = '<option value="">All groups</option>' +
      groups.map(g => `<option value="${{escapeHtml(g)}}">${{escapeHtml(g)}}</option>`).join("");
    groupSel.addEventListener("change", () => {{ activeGroup = groupSel.value; render(); }});
  }}

  const levels = Array.from(new Set(ROWS.flatMap(r => (r.levels && r.levels.length) ? r.levels : ["-"]))).sort();
  const chipBox = document.getElementById("levelChips");
  chipBox.innerHTML = levels.map(lv =>
    `<span class="chip" data-level="${{escapeHtml(lv)}}">${{escapeHtml(lv)}}</span>`
  ).join("");
  chipBox.querySelectorAll(".chip").forEach(chip => {{
    chip.addEventListener("click", () => {{
      const lv = chip.dataset.level;
      if (activeLevels.has(lv)) {{ activeLevels.delete(lv); chip.classList.remove("active"); }}
      else {{ activeLevels.add(lv); chip.classList.add("active"); }}
      render();
    }});
  }});

  document.getElementById("search").addEventListener("input", (e) => {{
    searchText = e.target.value.toLowerCase();
    render();
  }});

  document.querySelectorAll("thead th").forEach(th => {{
    th.addEventListener("click", () => {{
      const key = th.dataset.key;
      if (sortKey === key) sortDir *= -1; else {{ sortKey = key; sortDir = -1; }}
      render();
    }});
  }});
}}

function matches(row) {{
  if (activeGroup && row.group !== activeGroup) return false;
  if (activeLevels.size) {{
    const rowLevels = (row.levels && row.levels.length) ? row.levels : ["-"];
    if (!rowLevels.some(lv => activeLevels.has(lv))) return false;
  }}
  if (searchText) {{
    const hay = (row.template + " " + (row.apps || []).join(" ") + " " + row.group).toLowerCase();
    if (!hay.includes(searchText)) return false;
  }}
  return true;
}}

function sortRows(rows) {{
  return rows.slice().sort((a, b) => {{
    let av = a[sortKey], bv = b[sortKey];
    if (sortKey === "apps") {{ av = fmtApps(a.apps); bv = fmtApps(b.apps); }}
    if (sortKey === "levels") {{ av = (a.levels||[]).join(); bv = (b.levels||[]).join(); }}
    if (av == null) av = "";
    if (bv == null) bv = "";
    if (av < bv) return -1 * sortDir;
    if (av > bv) return 1 * sortDir;
    return 0;
  }});
}}

function render() {{
  const filtered = sortRows(ROWS.filter(matches));
  const tbody = document.getElementById("rows");
  document.getElementById("emptyMsg").classList.toggle("hidden", filtered.length > 0);
  tbody.innerHTML = filtered.map((row, i) => {{
    const levels = (row.levels && row.levels.length) ? row.levels : ["-"];
    const levelBadges = levels.map(lv => `<span class="badge ${{levelClass(lv)}}">${{escapeHtml(lv)}}</span>`).join("");
    const flags = [];
    if (row.count === 1) flags.push('<span class="badge single">SINGLE</span>');
    if (row.unrecognized) flags.push('<span class="badge unrecognized">UNRECOGNIZED</span>');
    const examples = (row.examples || []).map(e => `<div class="ex">${{escapeHtml(e)}}</div>`).join("");
    const allTs = (row.all_timestamps || []).join(", ");
    return `
      <tr class="main" data-idx="${{i}}">
        <td class="count">${{row.count.toLocaleString()}} ${{flags.join(" ")}}</td>
        <td class="apps">${{escapeHtml(row.group)}}</td>
        <td class="apps">${{escapeHtml(fmtApps(row.apps))}}</td>
        <td>${{levelBadges}}</td>
        <td class="template">${{escapeHtml(row.template)}}</td>
        <td class="when">${{fmtWhen(row)}}</td>
      </tr>
      <tr class="detail hidden" data-detail-for="${{i}}">
        <td colspan="6">
          <div><strong>Examples:</strong></div>
          ${{examples || '<div class="ex">(none captured)</div>'}}
          ${{allTs ? `<div class="ts"><strong>All timestamps:</strong> ${{escapeHtml(allTs)}}</div>` : ""}}
        </td>
      </tr>`;
  }}).join("");

  tbody.querySelectorAll("tr.main").forEach(tr => {{
    tr.addEventListener("click", () => {{
      const idx = tr.dataset.idx;
      tbody.querySelector(`tr[data-detail-for="${{idx}}"]`).classList.toggle("hidden");
    }});
  }});
}}

buildStats();
buildControls();
render();
</script>
</body>
</html>
"""


def build_html(rows, source_label):
    n_groups = len({r.get("group") for r in rows})
    subtitle = f"{source_label} &mdash; {len(rows)} templates" + (
        f" across {n_groups} app groups" if n_groups > 1 else ""
    )
    return PAGE_TEMPLATE.format(
        title_suffix=f" — {html.escape(source_label)}",
        subtitle=subtitle,
        rows_json=json.dumps(rows),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", required=True, help="Path to a mine_templates.py --output JSON file")
    parser.add_argument("--output", default=None, help="Path to write the HTML report (default: <results>.html)")
    args = parser.parse_args()

    results_path = Path(args.results)
    rows = load_rows(results_path)
    out_path = Path(args.output) if args.output else results_path.with_suffix(".html")
    out_path.write_text(build_html(rows, results_path.name), encoding="utf-8")
    print(f"Wrote {out_path} ({len(rows)} templates) -- open it directly in a browser.")


if __name__ == "__main__":
    main()
