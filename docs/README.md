# Diagrams

Two JPEGs, drawn in the control panel's own dark palette (the `:root`
values in `web/public/index.html`) so they read as part of the same system.

| file | what it shows |
|---|---|
| `architecture.jpg` | the stack: browser -> web -> aura -> four MCP servers -> what they reach |
| `proxy-filters.jpg` | every hard filter `mezmo-proxy` injects, and the net effect |

## Regenerating

`proxy-filters.jpg` reads the filter constants **out of
`services/mezmo-proxy/server.py`**, so it cannot drift from the code — but
it does not update itself. Re-run it whenever the filters change:

```bash
pip install Pillow
```

```bash
cd docs && python _arch.py architecture.jpg && python _filters.py ../services/mezmo-proxy/server.py proxy-filters.jpg
```

The prose in `_filters.py` (volumes, rationale) is hand-written and is the
one part that can go stale — check it against the comments in `server.py`
if you change an exclusion.

Fonts fall back gracefully, but these were rendered with Segoe UI and
Consolas on Windows; on another OS the layout shifts slightly.
