# AgentWebView

**Give every AI agent a budget, a set of rules and a receipt for everything it does.**

AgentWebView runs on your own computer, between your AI agents (Claude Code, Cursor, VS Code, Claude Desktop,
or your own) and the tools they use:

- **Web search** through the provider keys you already have: Exa, Tavily, Firecrawl, Parallel, Linkup, SerpApi,
  You.com. Most have a free tier. AgentWebView **spends the credit that expires soonest first** and **switches
  provider when one runs out** (or is down, or rate-limits you).
- **Page reading**: a built-in reader that refuses private networks, or Firecrawl / Jina Reader.
- **Your own documents**: import files or crawl a website into a collection; agents search it with full-text search.
- **A sandboxed browser** (optional): typed actions only, every request through AgentWebView's proxy, and **your
  approval before any click that could submit, send, buy, delete or publish**.

Every call is checked against **hard limits before it happens** (per agent, per provider key, and overall) and
recorded with its sources, cost and routing decision on the **Activity** screen.

No account, no sign-up, no cloud: one process, one SQLite file, a dashboard at `http://127.0.0.1:8718`.

---

## Quick start

Requirements: Python 3.11 or newer, macOS or Linux.

```bash
git clone https://github.com/mannysinghx/AgentWebView.git && cd AgentWebView
python3 -m venv .venv && . .venv/bin/activate
pip install .
agentwebview serve --open
```

Then, in the dashboard:

1. **Providers** → add one or more API keys. The free tier is filled in for you (you can change it); keys are
   encrypted on your computer.
2. **Agents** → create an agent. Copy its token (shown once) and the setup line for your tool, for example for
   Claude Code:

   ```bash
   claude mcp add --transport http agentwebview http://127.0.0.1:8718/mcp --header "Authorization: Bearer awv_…"
   ```

3. Ask your agent: *"Use agentwebview to search the web for …"*, then open **Activity** to see the receipt.

Want the optional browser too?

```bash
pip install '.[browser]'
playwright install chromium
```

…and turn it on under **Settings → Browser**.

## Connecting agents

Each agent gets its own token (`awv_…`), abilities and limits. The dashboard shows ready-to-paste setup for each
client; for reference:

| Client | How |
|---|---|
| Claude Code | `claude mcp add --transport http agentwebview http://127.0.0.1:8718/mcp --header "Authorization: Bearer <token>"` |
| Cursor | `~/.cursor/mcp.json`: `{"mcpServers": {"agentwebview": {"url": "http://127.0.0.1:8718/mcp", "headers": {"Authorization": "Bearer <token>"}}}}` |
| VS Code | `.vscode/mcp.json`: `{"servers": {"agentwebview": {"type": "http", "url": "http://127.0.0.1:8718/mcp", "headers": {"Authorization": "Bearer <token>"}}}}` |
| Claude Desktop (stdio) | `{"mcpServers": {"agentwebview": {"command": "agentwebview", "args": ["mcp"], "env": {"AGENTWEBVIEW_TOKEN": "<token>"}}}}` |
| Any HTTP client | `POST http://127.0.0.1:8718/v1/tasks` with `Authorization: Bearer <token>` |

`agentwebview mcp` (stdio) runs the tools in its own process against the same data folder, so it works even when
`agentwebview serve` is not running.

### MCP tools

| Tool | What it does |
|---|---|
| `search` | Your documents (default) or, with `scope: "web"`, the web through your providers. Ranked evidence with source URLs and a coverage statement. |
| `read_page` | One URL's main text, or a stored document by `source_id`. |
| `crawl_site` | Crawl a site into a collection in the background (needs the *crawl* ability). |
| `add_document` | Save text into a collection (needs the *add documents* ability). |
| `browser_action` | `open`, `navigate`, `click`, `fill`, `select`, `scroll`, `extract`, `screenshot`, `wait`, `status`, `close` (needs the *browser* ability). |
| `task_status` | State and receipt of a task (crawls run in the background). |
| `routing_info` | Which engines and keys would answer, in what order, and why others are excluded. Runs nothing. |
| `rate_result` | Label a result relevant / partial / irrelevant; after five labels, routing uses them as quality. |

### REST API

```bash
curl -s http://127.0.0.1:8718/v1/tasks \
  -H "Authorization: Bearer $AGENTWEBVIEW_TOKEN" -H "Content-Type: application/json" \
  -d '{"operation": "search", "input": {"query": "MCP authorization spec"}, "requirements": {"coverage": "web_index"}}'
```

Operations: `search` (coverage `owned_corpus` or `web_index`), `read_page` (`url` or `source_id`), `crawl`
(returns `202` and a `task_id`). Also: `GET /v1/tasks/{id}?wait_ms=`, `POST /v1/tasks/{id}/cancel`,
`POST /v1/tasks/{id}/rating`, `POST /v1/route/preview`, `GET /v1/engines`, `GET /v1/me`, `GET /v1/collections`,
`POST /v1/collections/{name}/documents`, and the browser endpoints under `/v1/browser/…`. Interactive reference:
`http://127.0.0.1:8718/docs`.

## Providers

| Provider | Used for | Free tier (pre-filled; check the provider's page) | Unit |
|---|---|---|---|
| Exa | web search | $10 of credit a month | USD |
| Tavily | web search | 1,000 credits a month | credits |
| Firecrawl | web search, page reading | 1,000 credits a month | credits |
| Jina Reader | page reading | 10M tokens, non-commercial only (enter your allowance) | tokens |
| Parallel | web search | $5 a month (card on file) | USD |
| Linkup | web search | up to $20 a month (eligible accounts) | USD |
| SerpApi | web search (Google results) | 250 searches a month | searches |
| You.com | web search | a one-time $10 balance | USD |

AgentWebView never creates provider accounts and never buys credit: you sign up on the provider's site and paste the
key. Tavily, Firecrawl and SerpApi keys are checked with a free call when you add them (and the plan size and
usage they report are used); the others are verified by their first real call.

**A key is only ever used inside the allowance you record for it.** When the allowance for the period is used up,
or the provider says the key is out of credit, AgentWebView moves to the next key or provider. Providers with
paid plans work the same way: record the credit your plan includes.

## How a call works

1. **Cache.** A fresh identical result is reused (it costs one request and no provider credit).
2. **Plan.** Every engine is checked: is it turned on, does it do this operation, does it cover what was asked
   (your documents, a live page, a web index), is it healthy (circuit breaker, rate-limit cooldown), would it fit
   inside every limit? Each exclusion is written down with its reason.
3. **Score.** Remaining engines are ranked by cost, latency and measured quality (Settings → Prefer: cheapest,
   balanced or best quality). Provider keys whose credit expires soonest go first; optionally web searches
   *take turns* across all keys with credit.
4. **Reserve, call, settle.** The call's cost is reserved against every applicable limit at once (or nothing
   is), the engine runs, and the reservation is replaced by what was actually used (as reported by the provider
   when it reports it).
5. **Check.** Deterministic quality checks (non-empty, not just navigation, provenance, freshness). A failed
   attempt falls back to the next engine, up to the attempt limit.
6. **Receipt.** The result, its sources, the cost, every attempt and the full route are recorded.

## Limits

- **Global** (Settings): requests, searches, pages read and browser time, per month and/or per day.
- **Per agent** (Agents): requests per month and per day.
- **Per provider key** (Providers): the credit on that key, renewing monthly, daily, or one-time, with an optional
  expiry date.

Months and days are counted in the time zone set under Settings (UTC by default).

## Security model

AgentWebView has **no login**. It is built for one person on their own computer:

- It listens on `127.0.0.1` only. `agentwebview serve --host 0.0.0.0` is refused unless you add `--allow-remote`;
  only do that behind your own authentication (a VPN, or a reverse proxy that requires a login).
- Requests with an unexpected `Host` header are refused (DNS-rebinding protection; add names with
  `--extra-host`). Dashboard API calls need an `X-AgentWebView: 1` header and a same-site origin, so other websites
  you visit cannot drive it.
- Agents need their token for every call; tokens are stored hashed. Provider keys are encrypted with AES-256-GCM
  using `secret.key` in the data folder.
- The page reader, the crawler and the browser proxy resolve each destination once and refuse private, loopback,
  link-local (cloud metadata), multicast and other non-public addresses, on ports 80, 443, 8080 and 8443 only.
  Redirects are re-checked hop by hop. robots.txt is respected by default.
- The browser runs Chromium headless in its own process group with every request forced through the proxy,
  limited to the session's hosts, killed at its time limit; there is no action that runs arbitrary JavaScript.
- Everything an agent retrieves is data, not instructions: receipts say so (`retrieved_content_is_data`).

## Data, backup and configuration

Everything lives in the data folder (`~/.agentwebview`, or `$AGENTWEBVIEW_DATA`, or `--data`):
`agentwebview.db` (SQLite), `secret.key` (encrypts provider keys) and `screenshots/`. To back up, stop AgentWebView and
copy the folder; keep `secret.key` with the database.

| Option | Environment | Default |
|---|---|---|
| `--host` | `AGENTWEBVIEW_HOST` | `127.0.0.1` |
| `--port` | `AGENTWEBVIEW_PORT` | `8718` |
| `--data` | `AGENTWEBVIEW_DATA` | `~/.agentwebview` |
| `--allow-remote` | `AGENTWEBVIEW_ALLOW_REMOTE=1` | off |
| `--extra-host` | `AGENTWEBVIEW_EXTRA_HOSTS` (comma-separated) | none |
| `--public-url` | `AGENTWEBVIEW_PUBLIC_URL` | `http://127.0.0.1:<port>` |
| | `AGENTWEBVIEW_CHROMIUM` (Chromium path) | Playwright's |
| | `AGENTWEBVIEW_CHROMIUM_NO_SANDBOX=1` (containers only) | sandbox on |

### Docker

```bash
docker compose up -d          # dashboard on http://127.0.0.1:8718, data in the agentwebview-data volume
```

The compose file publishes the port on `127.0.0.1` only. Build with `--build-arg WITH_BROWSER=1` to include
Chromium (it then runs without Chromium's own sandbox, inside the container).

## Development

```bash
pip install -e '.[dev,browser]' && playwright install chromium
pytest
```

Tests run the real HTTP API with fake provider APIs and a local website reached through the real egress guard;
the browser test (skipped without Chromium) drives a real Chromium through the proxy and the approval flow.

The code is small and plain: `providers/adapters.py` (one class per provider), `routing.py` (eligibility,
scoring, health, quality checks), `limits.py` (reservations), `executor.py` (the call lifecycle), `service.py`
(everything the API calls), `api.py` and `mcp_server.py` (transports), `web/` (the dashboard, no build step).

Adding a provider means one `ProviderEngine` subclass with its request/response mapping, its unit, its error
codes for "out of credit", and (if it has one) a free key check; then add it to `ENGINE_CLASSES`.

## What it does not do

- No semantic (embedding) search over your documents; full-text only.
- No PDFs or Office files yet (`.txt`, `.md`, `.html`).
- No paid overage: a key is used only within the credit you record.
- No multi-user accounts, teams or remote access control. For that, run it behind your own login.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
