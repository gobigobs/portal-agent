"""Demo legacy vendor portal — stdlib-only HTTP server.

A fake "partner portal" for the portal-agent demo. It deliberately mimics
a legacy portal with a split personality:

* /api/orders   -- WORKS. Returns the 8 most recent orders as JSON.
                  (The API is capped at recent records; the full list is
                  only on the HTML page — a common legacy-portal quirk.)
* /api/payouts  -- BROKEN BY DESIGN. Always returns HTTP 500 with a JSON
                  error body, simulating a dead legacy payout service.
                  Payouts are only available on the HTML /payouts page,
                  so the agent must fall back to the browser for them.

Pages: /login, /dashboard, /orders, /payouts. All pages except /login
require the demo session cookie set by a successful login.

Run:  python3 -m portal.server [--port 8765]
Deps: none (Python 3.12 standard library only).
Demo credentials: demo / demo123 (demo-only, see README).
"""

import argparse
import html
import json
import sys
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from portal.data import DEMO_PASSWORD, DEMO_USERNAME, ORDERS, PAYOUTS

SESSION_COOKIE_NAME = "portal_session"
# Fixed demo session token. This is a toy; there is no real auth here.
SESSION_TOKEN = "demo-session-token"

CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
margin:0;background:#f6f4ef;color:#1d1a16}
header{background:#7a1f1f;color:#fff;padding:14px 24px;display:flex;
justify-content:space-between;align-items:center}
header a{color:#ffd9a8;text-decoration:none;margin-left:16px}
main{max-width:960px;margin:24px auto;padding:0 16px 48px}
h1{font-size:26px;margin:8px 0 4px} h2{font-size:20px;margin-top:28px}
p.muted{color:#6b6259}
.card{background:#fff;border:1px solid #e3ddd2;border-radius:12px;
padding:16px 20px;margin:12px 0}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.cards .card b{font-size:26px;display:block}
.notice{background:#fff4d6;border:1px solid #e8c96a;border-radius:10px;
padding:12px 16px;margin:14px 0}
.error{background:#fde8e8;border:1px solid #e0a0a0;border-radius:10px;
padding:12px 16px;margin:14px 0}
table{width:100%;border-collapse:collapse;background:#fff}
th,td{border:1px solid #e3ddd2;padding:8px 10px;text-align:left;font-size:14px}
th{background:#7a1f1f;color:#fff}
tr:nth-child(even) td{background:#faf7f1}
label{display:block;margin:10px 0 4px;font-weight:600}
input[type=text],input[type=password]{width:280px;padding:9px 10px;
border:1px solid #c9c0b2;border-radius:8px;font-size:15px}
button{background:#7a1f1f;color:#fff;border:0;border-radius:8px;
padding:10px 20px;font-size:15px;cursor:pointer}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;
background:#e8f3e8;color:#1e5c1e}
.badge.cancelled{background:#fde8e8;color:#8a1f1f}
.badge.preparing,.badge.pending,.badge.processing{background:#fff4d6;color:#7a5b00}
code{background:#efe9dd;padding:1px 6px;border-radius:5px}
footer{color:#6b6259;font-size:13px;text-align:center;padding:20px}
"""

BASE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>{title} — Atti Vendors Portal (DEMO)</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{css}</style></head><body>
<header><div><b>Atti Vendors</b> &nbsp;Partner Portal &nbsp;<span>DEMO</span></div>
<nav>{nav}</nav></header><main>{body}</main>
<footer>Demo portal for the portal-agent project. All data is synthetic.
No real orders, customers, or money.</footer></body></html>"""


def page(title, body, nav=True):
    nav_html = ""
    if nav:
        nav_html = ('<a href="/dashboard">Dashboard</a>'
                    '<a href="/orders">Orders</a>'
                    '<a href="/payouts">Payouts</a>'
                    '<a href="/logout">Logout</a>')
    return BASE.format(title=html.escape(title), css=CSS,
                       nav=nav_html, body=body)


def orders_table(orders):
    rows = "".join(
        "<tr><td>{o[order_id]}</td><td>{o[date]}</td><td>{o[customer]}</td>"
        "<td>{o[channel]}</td><td>{o[items]}</td><td>&#8377;{o[amount_inr]}</td>"
        "<td><span class=\"badge {cls}\">{o[status]}</span></td></tr>".format(
            o={k: html.escape(str(v)) for k, v in order.items()},
            cls=order["status"].lower())
        for order in orders)
    return ("<table id=\"orders-table\"><thead><tr><th>Order</th><th>Date</th>"
            "<th>Customer</th><th>Channel</th><th>Items</th><th>Amount</th>"
            "<th>Status</th></tr></thead><tbody>" + rows + "</tbody></table>")


def payouts_table(payouts):
    rows = "".join(
        "<tr><td>{p[payout_id]}</td><td>{p[settlement_date]}</td>"
        "<td>{p[orders_included]}</td><td>&#8377;{p[gross_inr]}</td>"
        "<td>&#8377;{p[fee_inr]}</td><td>&#8377;{p[net_inr]}</td>"
        "<td><span class=\"badge {cls}\">{p[status]}</span></td>"
        "<td>{p[utr]}</td></tr>".format(
            p={k: html.escape(str(v)) for k, v in pay.items()},
            cls=pay["status"].lower())
        for pay in payouts)
    return ("<table id=\"payouts-table\"><thead><tr><th>Payout</th>"
            "<th>Settlement date</th><th>Orders</th><th>Gross</th><th>Fee</th>"
            "<th>Net</th><th>Status</th><th>UTR</th></tr></thead><tbody>"
            + rows + "</tbody></table>")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- helpers ---------------------------------------------------------
    def log_message(self, fmt, *args):  # keep stdout clean; agent logs steps
        sys.stderr.write("[portal] %s - %s\n" % (self.address_string(),
                                                  fmt % args))

    def _send(self, status, body, content_type="text/html; charset=utf-8",
              extra_headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_json(self, status, obj):
        self._send(status, json.dumps(obj, indent=2),
                   "application/json; charset=utf-8")

    def _logged_in(self):
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        morsel = cookie.get(SESSION_COOKIE_NAME)
        return morsel is not None and morsel.value == SESSION_TOKEN

    def _require_login(self):
        if not self._logged_in():
            self._send(302, "", extra_headers={"Location": "/login"})
            return False
        return True

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(302, "", extra_headers={"Location": "/login"})
        elif path == "/login":
            self._login_page()
        elif path == "/logout":
            self._send(302, "Logged out.",
                       extra_headers={"Location": "/login",
                                      "Set-Cookie": f"{SESSION_COOKIE_NAME}=;"
                                      " Path=/; Max-Age=0"})
        elif path == "/api/health":
            self._send_json(200, {"status": "ok", "portal": "demo"})
        elif path == "/api/orders":
            self._api_orders()
        elif path == "/api/payouts":
            self._api_payouts()
        elif path == "/dashboard":
            if self._require_login():
                self._dashboard()
        elif path == "/orders":
            if self._require_login():
                self._orders_page()
        elif path == "/payouts":
            if self._require_login():
                self._payouts_page()
        else:
            self._send(404, page("Not found",
                                 "<h1>404</h1><p>That page does not exist "
                                 "in this demo portal.</p>"))

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            user = form.get("username", [""])[0]
            pw = form.get("password", [""])[0]
            if user == DEMO_USERNAME and pw == DEMO_PASSWORD:
                self._send(302, "", extra_headers={
                    "Location": "/dashboard",
                    "Set-Cookie": f"{SESSION_COOKIE_NAME}={SESSION_TOKEN}; "
                                  "Path=/; HttpOnly"})
            else:
                self._login_page(error="Wrong username or password. "
                                       "Demo credentials: demo / demo123")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    # -- pages -----------------------------------------------------------
    def _login_page(self, error=None):
        err = (f'<div class="error">{html.escape(error)}</div>'
               if error else "")
        body = (f"<h1>Vendor login</h1>"
                f"<p class=\"muted\">Atti Vendors partner portal — "
                f"demo build.</p>{err}"
                '<div class="card"><form method="post" action="/login">'
                '<label for="username">Username</label>'
                '<input id="username" name="username" type="text" '
                'autocomplete="username">'
                '<label for="password">Password</label>'
                '<input id="password" name="password" type="password" '
                'autocomplete="current-password">'
                '<div style="margin-top:16px"><button type="submit">'
                'Log in</button></div></form></div>'
                '<p class="muted">Demo credentials: '
                '<code>demo</code> / <code>demo123</code> '
                '(demo-only, not a real account).</p>')
        self._send(200, page("Login", body, nav=False))

    def _dashboard(self):
        total = sum(o["amount_inr"] for o in ORDERS)
        delivered = sum(1 for o in ORDERS if o["status"] == "Delivered")
        pending_net = sum(p["net_inr"] for p in PAYOUTS
                          if p["status"] != "Settled")
        body = (f"<h1>Dashboard</h1><p class=\"muted\">Welcome, demo vendor. "
                "Today is 25 Nov 2026 in demo data.</p>"
                '<div class="cards">'
                f'<div class="card">Total orders<b>{len(ORDERS)}</b></div>'
                f'<div class="card">Delivered<b>{delivered}</b></div>'
                f'<div class="card">Order value<b>&#8377;{total:,}</b></div>'
                f'<div class="card">Payouts pending<b>&#8377;{pending_net:,}</b></div>'
                "</div>"
                '<div class="notice"><b>API status:</b> Orders API is up. '
                "Payouts API is <b>down</b> — the legacy payout service "
                "returns HTTP 500. Payouts are only on the "
                '<a href="/payouts">Payouts page</a>.</div>'
                "<h2>Recent orders</h2>" + orders_table(ORDERS[:5]) +
                '<p><a href="/orders">See all orders &rarr;</a> &nbsp; '
                '<a href="/payouts">See payouts &rarr;</a></p>')
        self._send(200, page("Dashboard", body))

    def _orders_page(self):
        body = ("<h1>Orders</h1><p class=\"muted\">All orders, newest first. "
                "The JSON API returns only the 8 most recent — "
                "the full list lives here, on this page.</p>"
                + orders_table(ORDERS))
        self._send(200, page("Orders", body))

    def _payouts_page(self):
        body = ("<h1>Payouts</h1><p class=\"muted\">Settlements from the "
                "legacy payout service. The payouts API is down, so this "
                "page is the only source.</p>" + payouts_table(PAYOUTS))
        self._send(200, page("Payouts", body))

    # -- API -------------------------------------------------------------
    def _api_orders(self):
        if not self._logged_in():
            self._send_json(401, {"error": "unauthorized",
                                  "detail": "Login first; session cookie "
                                            "required."})
            return
        recent = ORDERS[:8]  # API cap: recent 8 only
        self._send_json(200, {
            "source": "api", "total_orders": len(ORDERS),
            "returned": len(recent),
            "note": "API returns the 8 most recent orders only. "
                    "Full list is on the /orders page.",
            "orders": recent})

    def _api_payouts(self):
        if not self._logged_in():
            self._send_json(401, {"error": "unauthorized",
                                  "detail": "Login first; session cookie "
                                            "required."})
            return
        # Broken by design: the legacy payout service is down.
        self._send_json(500, {
            "error": "legacy_payout_service_unavailable",
            "detail": "Payout service timed out after 30s. "
                      "Use the /payouts page in the browser instead.",
            "payouts": None})


def main():
    ap = argparse.ArgumentParser(description="Demo legacy vendor portal")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[portal] Demo vendor portal on http://{args.host}:{args.port}",
          flush=True)
    print("[portal] Demo login: demo / demo123  (demo-only)", flush=True)
    print("[portal] API: /api/orders works, /api/payouts returns HTTP 500 "
          "by design", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
