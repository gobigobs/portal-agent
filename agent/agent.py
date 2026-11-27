"""Portal agent — browser agent for a portal with no (working) API.

What it does, in order:
  1. Opens the demo vendor portal in headless Chromium (Playwright)
     and logs in with the demo credentials.
  2. Tries the JSON API first, reusing the browser session cookie:
     GET /api/orders  -> works, returns the 8 most recent orders.
  3. Tries GET /api/payouts -> the legacy payout service answers
     HTTP 500. The agent logs an explicit FALLBACK_DECISION and
     switches to the browser: it navigates to /payouts and extracts
     the 6 payout records from the HTML table instead.
  4. Also extracts the full 12-order list from the /orders page in
     the browser (the API only returns the recent 8) and cross-checks
     that every API order appears in the browser list.
  5. Writes structured output + a timestamped step log, with a real
     Playwright screenshot saved for every step.

Outputs (default dirs: output/, screenshots/):
  output/records.json          combined extraction (orders + payouts)
  output/orders_api.json       the 8 orders returned by the API
  output/orders_browser.json   all 12 orders scraped from /orders
  output/payouts_browser.json  the 6 payouts scraped from /payouts
  output/orders.csv / payouts.csv
  output/run_log.json          every step, with timestamps + screenshots

Demo credentials only: demo / demo123 (override with env
PORTAL_USER / PORTAL_PASS for a different demo portal). Never use
real portal credentials here — this agent stores none.

Deps: playwright (+ a Chromium build; see README / run.sh).
Run:  python3 -m agent.agent [--base-url http://127.0.0.1:8765]
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_USER = os.environ.get("PORTAL_USER", "demo")
DEFAULT_PASS = os.environ.get("PORTAL_PASS", "demo123")


def now_iso():
    return datetime.now(IST).isoformat(timespec="seconds")


def inr(text):
    """'₹1,089' -> 1089 ; '2' -> 2."""
    return int(str(text).replace("\u20b9", "").replace(",", "").strip())


class StepLog:
    """Timestamped step log; every step may carry a screenshot path."""

    def __init__(self):
        self.steps = []

    def add(self, action, detail="", status="ok", screenshot=None):
        step = {"step": len(self.steps) + 1, "time_ist": now_iso(),
                "action": action, "detail": detail, "status": status,
                "screenshot": screenshot}
        self.steps.append(step)
        shot = f" [shot: {screenshot}]" if screenshot else ""
        print(f"[{step['step']:02d}] {step['time_ist']} {status.upper():7s} "
              f"{action} — {detail}{shot}", flush=True)
        return step

    def save(self, path):
        Path(path).write_text(json.dumps(
            {"generated_at_ist": now_iso(), "steps": self.steps}, indent=2),
            encoding="utf-8")


def launch_browser(pw, log):
    """Launch headless Chromium. Tries the Playwright bundle first,
    then a system Chromium, then PLAYWRIGHT_CHROMIUM_PATH.
    Raises RuntimeError with a plain explanation if nothing works —
    the caller reports the browser path as unverified, never fakes it.
    """
    attempts = [("playwright bundled chromium", {}),
                ("system chromium channel", {"channel": "chromium"})]
    env_path = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH")
    if env_path:
        attempts.append((f"PLAYWRIGHT_CHROMIUM_PATH={env_path}",
                         {"executable_path": env_path}))
    else:
        for cand in sorted(Path.home().glob(
                ".cache/ms-playwright/chromium-*/chrome-linux*/chrome")):
            attempts.append((f"cached chromium at {cand}",
                             {"executable_path": str(cand)}))
    last_err = None
    for name, kwargs in attempts:
        try:
            browser = pw.chromium.launch(headless=True, **kwargs)
            log.add("browser_launch", f"Headless Chromium via {name}")
            return browser
        except Exception as exc:  # noqa: BLE001 - try next candidate
            last_err = exc
            log.add("browser_launch_retry",
                    f"{name} failed: {exc}", status="failed")
    raise RuntimeError(
        "No Chromium could be launched. Tried the Playwright bundle, "
        "the system chromium channel, and any cached build. Run "
        "'python -m playwright install chromium' with network access, "
        "or set PLAYWRIGHT_CHROMIUM_PATH. "
        f"Last error: {last_err}")


def extract_table_rows(page, table_selector):
    """Read a <table> into a list of dicts keyed by its header text."""
    table = page.locator(table_selector)
    table.wait_for(timeout=10000)
    headers = [h.strip().lower().replace(" ", "_")
               for h in table.locator("thead th").all_inner_texts()]
    rows = []
    for tr in table.locator("tbody tr").all():
        cells = [c.strip() for c in tr.locator("td").all_inner_texts()]
        rows.append(dict(zip(headers, cells)))
    return rows


def normalise_orders(rows):
    return [{"order_id": r["order"], "date": r["date"],
             "customer": r["customer"], "channel": r["channel"],
             "items": r["items"], "amount_inr": inr(r["amount"]),
             "status": r["status"]} for r in rows]


def normalise_payouts(rows):
    return [{"payout_id": r["payout"], "settlement_date": r["settlement_date"],
             "orders_included": int(r["orders"]),
             "gross_inr": inr(r["gross"]), "fee_inr": inr(r["fee"]),
             "net_inr": inr(r["net"]),
             "status": r["status"], "utr": r["utr"]} for r in rows]


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2), encoding="utf-8")


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run(base_url, out_dir, shots_dir, username, password):
    from playwright.sync_api import sync_playwright

    out_dir = Path(out_dir)
    shots_dir = Path(shots_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    shots_dir.mkdir(parents=True, exist_ok=True)
    log = StepLog()

    def shot(page, name):
        path = shots_dir / name
        page.screenshot(path=str(path))
        return str(path.relative_to(REPO_ROOT)) \
            if path.is_relative_to(REPO_ROOT) else str(path)

    with sync_playwright() as pw:
        browser = launch_browser(pw, log)
        try:
            context = browser.new_context(viewport={"width": 1280,
                                                    "height": 860})
            page = context.new_page()

            # 1 — Login page.
            page.goto(f"{base_url}/login", wait_until="networkidle")
            s1 = shot(page, "01_login_page.png")
            log.add("open_login_page", f"GET {base_url}/login",
                    screenshot=s1)

            # 2 — Log in with the demo credentials.
            page.fill("#username", username)
            page.fill("#password", password)
            page.click("button[type=submit]")
            page.wait_for_url(f"{base_url}/dashboard", timeout=10000)
            s2 = shot(page, "02_dashboard.png")
            log.add("login", f"Logged in as demo user '{username}' "
                             "(demo-only credentials); dashboard loaded",
                    screenshot=s2)

            # 3 — API path first: orders. Reuses the browser session
            #     cookie via the request context.
            api = context.request
            resp = api.get(f"{base_url}/api/orders", timeout=10000)
            if resp.status != 200:
                raise RuntimeError(
                    f"/api/orders returned HTTP {resp.status}: "
                    f"{resp.text()[:200]}")
            orders_api = resp.json()
            write_json(out_dir / "orders_api.json", orders_api)
            s3 = shot(page, "03_api_orders_ok.png")
            log.add("api_orders",
                    f"API path OK: GET /api/orders -> HTTP 200, "
                    f"{orders_api['returned']} of "
                    f"{orders_api['total_orders']} orders returned "
                    f"(API cap: recent 8 only). Saved orders_api.json.",
                    screenshot=s3)

            # 4 — API path for payouts: the realistic failure.
            resp = api.get(f"{base_url}/api/payouts", timeout=10000)
            s4 = shot(page, "04_api_payouts_failed.png")
            if resp.status == 200:
                raise RuntimeError(
                    "Expected /api/payouts to fail (HTTP 500 by design) "
                    "but it returned 200 — the demo portal changed.")
            error_body = resp.json()
            log.add("api_payouts_failed",
                    f"API path FAILED: GET /api/payouts -> "
                    f"HTTP {resp.status} "
                    f"({error_body.get('error')}).",
                    status="failed", screenshot=s4)
            log.add("FALLBACK_DECISION",
                    "API payouts unavailable "
                    f"({error_body.get('error')}: "
                    f"{error_body.get('detail')}). Decision: switch to "
                    "browser navigation — extract payouts from the "
                    "/payouts HTML table instead. Extraction continues; "
                    "no records lost.",
                    status="fallback")

            # 5 — Browser fallback: payouts page.
            page.goto(f"{base_url}/payouts", wait_until="networkidle")
            payout_rows = extract_table_rows(page, "#payouts-table")
            payouts = normalise_payouts(payout_rows)
            write_json(out_dir / "payouts_browser.json", payouts)
            s5 = shot(page, "05_payouts_page.png")
            log.add("browser_payouts",
                    f"Browser fallback OK: /payouts table extracted, "
                    f"{len(payouts)} payouts "
                    f"(net total \u20b9{sum(p['net_inr'] for p in payouts):,}). "
                    f"Saved payouts_browser.json.",
                    screenshot=s5)

            # 6 — Browser: full orders list (API only gave the recent 8).
            page.goto(f"{base_url}/orders", wait_until="networkidle")
            order_rows = extract_table_rows(page, "#orders-table")
            orders = normalise_orders(order_rows)
            write_json(out_dir / "orders_browser.json", orders)
            s6 = shot(page, "06_orders_page.png")
            log.add("browser_orders",
                    f"Browser extraction OK: /orders table extracted, "
                    f"{len(orders)} orders "
                    f"(value \u20b9{sum(o['amount_inr'] for o in orders):,}). "
                    f"Saved orders_browser.json.",
                    screenshot=s6)

            # 7 — Cross-check API vs browser, then write combined output.
            api_ids = {o["order_id"] for o in orders_api["orders"]}
            browser_ids = {o["order_id"] for o in orders}
            missing = sorted(api_ids - browser_ids)
            if missing:
                raise RuntimeError(
                    f"Cross-check failed: API orders missing from "
                    f"browser list: {missing}")
            if len(orders) != 12 or len(payouts) != 6 \
                    or len(orders_api["orders"]) != 8:
                raise RuntimeError(
                    f"Unexpected counts — API orders: "
                    f"{len(orders_api['orders'])} (expected 8), browser "
                    f"orders: {len(orders)} (expected 12), payouts: "
                    f"{len(payouts)} (expected 6).")
            combined = {
                "portal": base_url,
                "generated_at_ist": now_iso(),
                "demo_data": True,
                "sources": {
                    "orders_via_api": {
                        "endpoint": "/api/orders", "status": 200,
                        "count": len(orders_api["orders"]),
                        "note": "Recent 8 only (API cap)."},
                    "payouts_via_browser_fallback": {
                        "endpoint": "/api/payouts",
                        "api_status": resp.status,
                        "api_error": error_body.get("error"),
                        "fallback_page": "/payouts",
                        "count": len(payouts)},
                    "orders_via_browser_full_list": {
                        "page": "/orders", "count": len(orders),
                        "cross_check": "all 8 API order IDs present "
                                       "in the 12-row browser list"},
                },
                "orders": orders,
                "payouts": payouts,
            }
            write_json(out_dir / "records.json", combined)
            write_csv(out_dir / "orders.csv", orders,
                      ["order_id", "date", "customer", "channel", "items",
                       "amount_inr", "status"])
            write_csv(out_dir / "payouts.csv", payouts,
                      ["payout_id", "settlement_date", "orders_included",
                       "gross_inr", "fee_inr", "net_inr", "status", "utr"])
            page.goto(f"{base_url}/dashboard", wait_until="networkidle")
            s7 = shot(page, "07_done_dashboard.png")
            log.add("verify_and_save",
                    "Cross-check passed: all 8 API orders found in the "
                    "12-row browser list. Wrote records.json, orders.csv, "
                    "payouts.csv. Totals: 12 orders "
                    f"(\u20b9{sum(o['amount_inr'] for o in orders):,}), "
                    f"6 payouts (net \u20b9{sum(p['net_inr'] for p in payouts):,}).",
                    screenshot=s7)
        finally:
            browser.close()

    log.save(out_dir / "run_log.json")
    print(f"\nDone. {len(log.steps)} steps logged to "
          f"{out_dir / 'run_log.json'}", flush=True)
    return log


def main():
    ap = argparse.ArgumentParser(description="Portal agent (demo)")
    ap.add_argument("--base-url", default="http://127.0.0.1:8765")
    ap.add_argument("--out-dir", default=str(REPO_ROOT / "output"))
    ap.add_argument("--shots-dir", default=str(REPO_ROOT / "screenshots"))
    ap.add_argument("--username", default=DEFAULT_USER)
    ap.add_argument("--password", default=DEFAULT_PASS)
    args = ap.parse_args()
    try:
        run(args.base_url, args.out_dir, args.shots_dir,
            args.username, args.password)
    except RuntimeError as exc:
        print(f"\nAGENT FAILED: {exc}", file=sys.stderr, flush=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
