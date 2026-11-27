# portal-agent — Browser Agent for a Portal with No API

A browser agent that logs into a legacy vendor portal, tries the JSON API
first, and falls back to real browser navigation when the API fails — with
every step logged and screenshotted. Built as a demonstrable portfolio
project (FDE project #6) for manual portal work like Zomato / Swiggy /
PetPooja partner portals, where half the data lives behind a login and
the "API" only sometimes works.

**Demo credentials: `demo` / `demo123` — demo-only, not a real account.**
They appear in the code and the README on purpose, labelled demo
everywhere. There are no secrets in this repo.

## What it does

1. **Logs in** to the demo portal in headless Chromium (Playwright).
2. **Tries the API first**, reusing the browser session cookie:
   `GET /api/orders` → HTTP 200, the 8 most recent orders.
3. **Hits the realistic failure:** `GET /api/payouts` → HTTP 500
   (`legacy_payout_service_unavailable` — the legacy payout service
   is down by design). The agent logs an explicit `FALLBACK_DECISION`
   and switches to the browser.
4. **Falls back to the browser:** extracts the 6 payout records from
   the `/payouts` HTML table, and the full 12-order list from the
   `/orders` page (the API only returns the recent 8).
5. **Cross-checks:** every API order ID must appear in the browser
   list, otherwise the run fails loudly instead of silently
   publishing wrong data.
6. **Writes structured output + a step log**, with a real Playwright
   screenshot for every step.

## Architecture

```
portal-agent/
├── run.sh                  # one command: portal -> agent -> outputs
├── requirements.txt        # playwright only
├── portal/
│   ├── server.py           # demo legacy portal (Python stdlib only, no deps)
│   └── data.py             # seeded demo data: 12 orders, 6 payouts
├── agent/
│   └── agent.py            # Playwright agent: login, API-first, browser fallback
├── output/                 # records.json, orders/payouts CSV + JSON, run_log.json
└── screenshots/            # 7 real Playwright screenshots, one per step
```

```
                ┌──────────────────────────────┐
                │  Demo portal (localhost:8765)│
                │  /login /dashboard /orders   │
                │  /payouts                    │
                │  /api/orders  → 200 (8 rows) │
                │  /api/payouts → 500 (by design)
                └──────▲───────────────▲───────┘
                       │ browser       │ API (same session cookie)
                ┌──────┴───────────────┴───────┐
                │  Agent (Python + Playwright) │
                │  login → API orders → API    │
                │  payouts 500 → FALLBACK →    │
                │  browser payouts + orders    │
                └──────┬───────────────┬───────┘
                       ▼               ▼
              output/records.json   screenshots/*.png
              output/*.csv          output/run_log.json
```

## How to run the demo

```bash
cd portal-agent
./run.sh
```

That creates a venv (`.venv`), installs Playwright, reuses the cached
Chromium build if present (otherwise `playwright install chromium`
downloads it once), starts the demo portal on
`http://127.0.0.1:8765`, runs the agent, then stops the portal.

Manual split, if you want to watch the portal:

```bash
python3 -m portal.server --port 8765   # terminal 1
python3 -m agent.agent                 # terminal 2 (needs .venv + playwright)
```

## Verified run (25 Nov 2026, this machine)

Ran `./run.sh` from a clean shell (fresh `.venv`, outputs deleted
first). Exit code 0. Python 3.12.3, Playwright 1.65.2, Chromium
v1255 (headless, already cached on this machine — no download needed).

| Step | Result |
|---|---|
| Login (demo/demo123) | OK — dashboard loaded |
| `GET /api/orders` | HTTP 200 — 8 of 12 orders (API cap: recent 8) |
| `GET /api/payouts` | HTTP 500 `legacy_payout_service_unavailable` |
| Fallback decision | Logged explicitly; switched to `/payouts` in the browser |
| Browser payouts | 6 payouts extracted, net total ₹5,182 |
| Browser orders | 12 orders extracted, total value ₹5,559 |
| Cross-check | All 8 API order IDs present in the 12-row browser list |
| Outputs | `records.json`, `orders.csv` (12 rows), `payouts.csv` (6 rows), `run_log.json` (9 steps) |
| Screenshots | 7 real PNGs, 1280×860, in `screenshots/` |

Console log of the verified run:

```
[01] browser_launch — Headless Chromium via playwright bundled chromium
[02] open_login_page — GET http://127.0.0.1:8765/login
[03] login — Logged in as demo user 'demo'; dashboard loaded
[04] api_orders — API path OK: GET /api/orders -> HTTP 200, 8 of 12 orders
[05] api_payouts_failed — API path FAILED: GET /api/payouts -> HTTP 500
[06] FALLBACK_DECISION — switch to browser navigation, extract from /payouts
[07] browser_payouts — Browser fallback OK: 6 payouts (net total ₹5,182)
[08] browser_orders — Browser extraction OK: 12 orders (value ₹5,559)
[09] verify_and_save — Cross-check passed; wrote records.json, CSVs
```

## Screenshots (from the verified run — real agent screenshots)

Login page:

![Login page](screenshots/01_login_page.png)

Dashboard after login:

![Dashboard](screenshots/02_dashboard.png)

Payouts page — reached by the browser fallback after the payouts API
returned HTTP 500:

![Payouts page](screenshots/05_payouts_page.png)

Full orders list in the browser (12 rows; the API only returns 8):

![Orders page](screenshots/06_orders_page.png)

## Outputs

- `output/records.json` — combined extraction: 12 orders + 6 payouts,
  with a `sources` block saying exactly which path produced each set.
- `output/orders_api.json` — raw API response (8 orders).
- `output/orders_browser.json` / `output/payouts_browser.json` — raw
  browser extractions (12 orders / 6 payouts).
- `output/orders.csv` / `output/payouts.csv` — the same data as CSV.
- `output/run_log.json` — all 9 steps with IST timestamps, status,
  and screenshot path per step.

## Honest limitations

- **Demo portal only.** The portal is a toy I built for this demo
  (stdlib Python, fixed session token, seeded fake data). It proves
  the agent pattern — login, API-first, browser fallback, logged
  extraction — not that it works against any real portal.
- **No real target portal tested.** Zomato / Swiggy / PetPooja were
  not touched. A real portal will differ: different login flow
  (OTP, CAPTCHA), different table markup, pagination, rate limits,
  and session expiry. Each needs a per-portal adapter, not a
  config tweak.
- **No CAPTCHA / OTP / 2FA handling.** The demo login is a plain
  username + password form. Real portals that require OTP or a
  CAPTCHA will block this agent as written.
- **Brittle selectors by design.** Extraction reads `#orders-table`
  / `#payouts-table` by header name. A markup change on a real
  portal breaks extraction — that is expected; the cross-check and
  the step log exist so a break is loud, not silent.
- **Credentials are demo-only and in the clear.** Real credentials
  must come from a vault / env vars and never be committed. The
  agent accepts `PORTAL_USER` / `PORTAL_PASS` env overrides, but
  that is still not a credential-management solution.
- **No retries or stealth.** One attempt per API call, no
  backoff, no human-like delays, no anti-bot handling. It is a
  demo, not a production scraper.
- **Screenshots 02/03/04/07 are the same dashboard page.** Steps
  3–4 are API calls made while the browser sits on the dashboard,
  so those screenshots intentionally show the dashboard at the
  time of each API call — they are not screenshots of the API
  response itself (an API call has no page to screenshot).
- **Environment note:** on this VM, `pip` needs the normal shell
  proxy variables to reach PyPI. Running `run.sh` under `env -i`
  (no proxy vars) fails at `pip install playwright` with an SSL
  error — that is an environment quirk, not a repo bug. Run it
  from a normal shell.
