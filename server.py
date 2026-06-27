#!/usr/bin/env python3
from http.server import SimpleHTTPRequestHandler, HTTPServer
import json
import os
import threading
import time
import urllib.request

# --- Hyperliquid Price Feed ---
_prices = {}
_lock = threading.Lock()
ASSETS = ["BTC", "ETH", "SOL", "XRP", "XLM"]

def _fetch_prices():
    while True:
        try:
            req = urllib.request.Request(
                "https://api.hyperliquid.xyz/info",
                data=json.dumps({"type": "allMids"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as res:
                mids = json.loads(res.read())
            with _lock:
                for asset in ASSETS:
                    if asset in mids:
                        _prices[asset] = float(mids[asset])
        except Exception as e:
            print("Price fetch error:", e)
        time.sleep(2)

threading.Thread(target=_fetch_prices, daemon=True).start()

# --- Submissions Storage ---
SUBMISSIONS_FILE = "submissions.json"

def _load_submissions():
    if os.path.exists(SUBMISSIONS_FILE):
        with open(SUBMISSIONS_FILE, "r") as f:
            return json.load(f)
    return []

def _save_submissions(data):
    with open(SUBMISSIONS_FILE, "w") as f:
        json.dump(data, f, indent=2)

# --- HTTP Handler ---
class BotHandler(SimpleHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/prices":
            with _lock:
                prices = dict(_prices)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps(prices).encode())
        elif self.path == "/api/submissions":
            subs = _load_submissions()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps({"count": len(subs), "submissions": subs}).encode())
        elif self.path == "/api/pending":
            subs = _load_submissions()
            pending = [s for s in subs if s.get("wallet") and not s.get("approved")]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps({"count": len(pending), "pending": pending}).encode())
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/contact":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                data["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
                data["approved"] = False
                data["wallet"] = ""
                subs = _load_submissions()
                subs.append(data)
                _save_submissions(subs)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self._cors()
                self.end_headers()
                self.wfile.write(json.dumps({"status": "saved", "id": len(subs), "next_step": "verify-wallet"}).encode())
            except Exception as e:
                self.send_response(500)
                self._cors()
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())

        elif self.path == "/api/verify-wallet":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                email = data.get("email")
                wallet = data.get("wallet")
                subs = _load_submissions()
                for s in subs:
                    if s.get("email") == email:
                        s["wallet"] = wallet
                        s["wallet_submitted"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
                        break
                _save_submissions(subs)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self._cors()
                self.end_headers()
                self.wfile.write(json.dumps({"status": "wallet_saved", "message": "Waiting for approval"}).encode())
            except Exception as e:
                self.send_response(500)
                self._cors()
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())

        elif self.path == "/api/approve":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                email = data.get("email")
                subs = _load_submissions()
                for s in subs:
                    if s.get("email") == email:
                        s["approved"] = True
                        s["approved_at"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
                        break
                _save_submissions(subs)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self._cors()
                self.end_headers()
                self.wfile.write(json.dumps({"status": "approved", "message": "User can now access dashboard"}).encode())
            except Exception as e:
                self.send_response(500)
                self._cors()
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())

        else:
            self.send_response(404)
            self.end_headers()

# --- Start Server ---
PORT = int(os.environ.get("PORT", 8000))
server = HTTPServer(("0.0.0.0", PORT), BotHandler)
print(f"A84 Crypto Bot running on port {PORT}")
server.serve_forever()
