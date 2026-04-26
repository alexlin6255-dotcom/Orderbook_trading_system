"""
proxy.py — Local proxy server for the OrderBook Dashboard
Run: python proxy.py
Then open: http://localhost:5000/trading_dashboard.html
"""

import http.server
import urllib.request
import urllib.error
import json
import os
import sys
from datetime import date, timedelta

PORT = int(os.environ.get("PORT", 8888))

# Always serve files from the same folder as this script
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

class ProxyHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"  {args[0]} {args[1]}")

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "":
            path = "/trading_dashboard.html"

        filepath = os.path.join(SCRIPT_DIR, path.lstrip("/"))

        if os.path.exists(filepath) and os.path.isfile(filepath):
            with open(filepath, "rb") as f:
                content = f.read()
            self.send_response(200)
            if filepath.endswith(".html"):
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif filepath.endswith(".js"):
                self.send_header("Content-Type", "application/javascript")
            elif filepath.endswith(".css"):
                self.send_header("Content-Type", "text/css")
            self.send_cors()
            self.end_headers()
            self.wfile.write(content)
        else:
            print(f"  404 Not found: {filepath}")
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

    def do_POST(self):
        if self.path != "/fetch-bars":
            self.send_response(404)
            self.end_headers()
            return

        length  = int(self.headers.get("Content-Length", 0))
        body    = json.loads(self.rfile.read(length))
        ticker  = body.get("ticker", "").upper().strip()
        api_key = body.get("api_key", "").strip()

        if not ticker or not api_key:
            self._err(400, "Missing ticker or api_key")
            return

        date_to   = date.today() - timedelta(days=1)
        date_from = date_to - timedelta(days=8)

        url = (
            f"https://api.massive.com/v2/aggs/ticker/{ticker}/range/1/minute"
            f"/{date_from}/{date_to}"
            f"?adjusted=true&sort=asc&limit=50000"
        )

        print(f"  Fetching {ticker} ...")

        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body_bytes = e.read()
            try:
                msg = json.loads(body_bytes).get("message", str(e))
            except Exception:
                msg = str(e)
            self._err(e.code, msg)
            return
        except urllib.error.URLError as e:
            self._err(503, f"Could not reach Massive API: {e.reason}")
            return
        except Exception as e:
            self._err(500, str(e))
            return

        results = data.get("results") or []
        if not results:
            self._err(404, f"No data for '{ticker}'. Symbol may be invalid or market was closed.")
            return

        print(f"  OK {ticker}: {len(results)} bars")
        self._ok({"results": results, "ticker": ticker, "count": len(results)})

    def _ok(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_cors()
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, message):
        body = json.dumps({"error": message}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_cors()
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print()
    print("=" * 52)
    print("  OrderBook Dashboard Proxy")
    print("=" * 52)
    print(f"  Serving files from: {SCRIPT_DIR}")
    print(f"  Dashboard:  http://127.0.0.1:{PORT}/trading_dashboard.html")
    print()
    print("  Keep this terminal open. Press Ctrl+C to stop.")
    print("=" * 52)
    print()

    # Check the HTML file exists
    html_path = os.path.join(SCRIPT_DIR, "trading_dashboard.html")
    if not os.path.exists(html_path):
        print(f"  WARNING: trading_dashboard.html not found in {SCRIPT_DIR}")
        print(f"  Make sure both files are in the same folder.")
        print()

    server = http.server.HTTPServer(("127.0.0.1", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        sys.exit(0)
