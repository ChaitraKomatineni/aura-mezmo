# Mezmo Export API (`/v2/export`) — reference

Research notes for the "fetch raw lines, cluster them ourselves" path, as an
alternative to Mezmo's MCP summarisation tools (see
`experiments/mcp-tool-behavior` for why those fall short: 2 of 8 never
succeed at fleet volume, and none can return raw occurrences).

Sources: Mezmo's own OpenAPI spec `https://docs.mezmo.com/apis/log-analysis.json`
(Log Analysis API v2.1), plus live probing of this account on 2026-09-20.
Where the two disagree, the live result wins and says so.

---

## BLOCKER: the key in `.env` does not work here

`MEZMO_API_KEY` (`sts_…`) is scoped to the MCP server. Measured against
`GET /v2/export` with valid parameters:

| Auth header | Result |
|---|---|
| `Authorization: Token sts_…` | **403** |
| `servicekey: sts_…` | **403** |
| `Authorization: Bearer sts_…` | 401 |

403 = recognised but not authorised for this endpoint. **A separate
credential is required before any of the below can be used.**

Per the spec there are three accepted mechanisms:

- **`Authorization: Token {access_token}`** — the current one. An IAM Access
  Token. This is what to create.
- `servicekey: {key}` — **deprecated**; the spec states service keys "can no
  longer be created, but can still be used for v1 and v2 apis while the
  transition takes place". So an *existing* service key would work; a new one
  cannot be minted.
- Basic auth, key as username and no password:
  `curl https://api.mezmo.com/v1/config/view -u KEY:`

`Bearer` is not accepted (401) — that scheme is MCP-only.

---

## Endpoint

```
GET https://api.mezmo.com/v2/export
```

### Parameters (all query-string)

| Param | Required | Notes |
|---|---|---|
| `from` | **yes** | UNIX timestamp, **seconds or milliseconds**, inclusive |
| `to` | **yes** | same, inclusive |
| `size` | no | number of log lines to return |
| `hosts` | no | comma-separated host list |
| `apps` | no | comma-separated app list |
| `levels` | no | comma-separated level list |
| `query` | no | Mezmo search query (same syntax as MCP) |
| `prefer` | no | `head` (earliest) or `tail` (latest). **Default `tail`** |
| `pagination_id` | no | page token; omit/empty on the first request |

`/v1/export` takes the same set plus `email` and `emailSubject`, which
deliver a download link by mail instead of streaming. v2 drops those and
adds `pagination_id`.

### Response

```json
{ "lines": [ … ], "pagination_id": "…" }
```

### Limits

- **10,000 lines per request.** Some Pro and Enterprise plans get 20,000.
- **Unlimited total via pagination** — this is the headline difference from
  MCP, which hard-caps at 1,000,000 lines *processed* and simply refuses.
- **Retention: 30 days** (measured via MCP's own error, which names the
  window explicitly).
- **Rate limits: not documented** in the spec and not yet measured. Assume
  they exist.

### Pagination

Pass no `pagination_id` on the first call. The response carries one; send it
on the next call. **Do not change any other parameter between pages** — the
token is bound to the original query.

---

## How to use it well

### 1. Prefer the structured filters over `query`

`hosts`, `apps` and `levels` are first-class parameters. Use them instead of
encoding the same thing in `query` wherever you can, for a reason that came
straight out of the MCP probing: **Mezmo does not reject a malformed query.
It returns zero results.** An unbalanced paren and a banned `*` wildcard both
returned `total: 0` rather than an error, which is indistinguishable from
"nothing matched". The structured parameters have no syntax to get wrong.

Note the consequence for our production scope: `levels` is a **whitelist**,
so "exclude DEBUG" becomes "include info,warn,error,fatal". That is safer
than `-level:debug` — a typo yields an obvious error instead of a silent
inversion.

### 2. Do the volume reduction in the request, not after

Unfiltered, 20 minutes of fleet traffic is ~3.5M lines ≈ 175–350 paginated
requests. With the same filtering the proxy already applies (drop DEBUG, drop
INFO from the two loudest apps) that falls roughly ten- to twenty-fold, into
single- or low-double-digit requests. The scope filter is what makes
extraction tractable, not merely tidy.

### 3. `prefer` matters when you cap `size`

With `size` set and no pagination, `tail` (the default) gives the **most
recent** lines in the window and `head` the earliest. For a triage question
about the start of an incident, the default silently gives you the wrong end.

### 4. Seconds vs milliseconds

`from`/`to` accept either. Be consistent; mixing units across paged requests
is a good way to get a subtly wrong window.

---

## Errors observed

| HTTP | Meaning |
|---|---|
| 400 | Bad request — malformed or missing parameters |
| 401 | Not authenticated — wrong auth scheme (e.g. `Bearer`) |
| 403 | Authenticated but not authorised for this endpoint — **what the MCP `sts_` key returns** |

The spec documents only 200 and 400 for this path; 401/403 are observed.

---

## Why this beats the MCP tools for our case

| | MCP tools | `/v2/export` |
|---|---|---|
| Raw lines | **not available at all** | yes |
| Volume ceiling | 1,000,000 lines, then refusal | 10–20k per page, unlimited by paging |
| Clustering | undocumented, untunable | ours (Drain3, `sim_th` tuned on evidence) |
| Drill into one template | not possible | yes, via our postings index |
| Coverage of a summary | ~20 templates/source, ~47% of lines | whatever we choose |
| Malformed input | silently returns empty | structured params can't be malformed |

`services/logs-mcp/template_mining.py` needs **no changes** to consume this:
`mine_file()` already takes a `Path`, so an export is written to a temp file
and mined, and the byte-offset postings index keeps working as-is.

## Next steps

1. Create an IAM Access Token in the Mezmo UI (Organisation → API keys).
   Service keys can no longer be created, so this is the only path.
2. Verify a real export comes back, and measure lines/second through
   pagination — that number decides whether this is interactive or batch.
3. Keep `get_log_histogram` (with an explicit `granularity`) and
   `group_logs_by_field` (`field=host`, ordinal only) from MCP as cheap
   server-side navigation; they answer "where and when" without pulling
   lines.
