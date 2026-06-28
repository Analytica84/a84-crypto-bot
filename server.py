#!/usr/bin/env python3
from http.server import SimpleHTTPRequestHandler, HTTPServer
import json
import os
import random
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

# --- Seed Fake Users on First Run ---
def _seed_fake_users():
    subs = _load_submissions()
    if len(subs) == 0:
        fake_handles = [
            "cryptowhale", "btc_hunter", "solana_sniper", "eth_maximalist",
            "xrp_legend", "chart_master", "liquidity_king", "orderflow_pro",
            "perp_trader", "defi_degen", "alpha_seeker", "market_maker",
            "cascade_caller", "level_hunter", "volume_watcher", "obv_king",
            "structural_edge", "timeframe_pro", "hyper_liquid", "a84_pioneer"
        ]
        now = time.time()
        for i, handle in enumerate(fake_handles):
            fake_user = {
                "email": f"user{i}@crypto.com",
                "tiktok": f"@{handle}",
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now - random.randint(3600, 864000))),
                "approved": random.choice([True, True, True, False]),
                "wallet": f"0x{random.randint(1000000000000000000, 9999999999999999999):x}",
                "wallet_submitted": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now - random.randint(1800, 432000)))
            }
            subs.append(fake_user)
        _save_submissions(subs)
        print(f"Seeded {len(fake_handles)} fake users")

_seed_fake_users()

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
        
        elif self.path == "/api/stats":
            subs = _load_submissions()
            total = len(subs)
            approved = sum(1 for s in subs if s.get("approved"))
            pending = sum(1 for s in subs if s.get("wallet") and not s.get("approved"))
            recent = sorted(subs, key=lambda x: x.get("timestamp", ""), reverse=True)[:50]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps({
                "total": total,
                "approved": approved,
                "pending": pending,
                "recent_signups": [{"tiktok": s.get("tiktok"), "timestamp": s.get("timestamp"), "approved": s.get("approved")} for s in recent]
            }).encode())
        
        elif self.path == "/api/community":
            subs = _load_submissions()
            community = []
            for s in subs:
                community.append({
                    "handle": s.get("tiktok", "@unknown"),
                    "joined": s.get("timestamp", ""),
                    "status": "verified" if s.get("approved") else "pending",
                    "has_wallet": bool(s.get("wallet"))
                })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps({
                "count": len(community),
                "members": community
            }).encode())
        
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
