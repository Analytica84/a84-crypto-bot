#!/usr/bin/env python3
"""A84 server - secured version.

Preserves: Hyperliquid price feed, /api/prices, signup/wallet/approve routes.
Fixes: fake-user seeding, public private-data routes, unprotected approve,
open file serving, wide-open CORS, no size/rate limits, leaked errors.

Config (Railway environment variables):
  ADMIN_TOKEN   required for admin routes. If unset, admin routes are disabled.
  ALLOWED_ORIGINS  optional, comma separated, e.g. https://www.analytica84.com
  DATA_DIR      optional, where submissions.json is stored (default: .)
  WHOP_API_KEY  Whop company API key (secret). Needed for the paid dashboard.
  WHOP_APP_ID   Whop app id (app_...). Needed for the paid dashboard.
If either WHOP value is missing, the paid dashboard stays closed (fails safe).
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import mimetypes
import os
import re
import threading
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
SUBMISSIONS_FILE = os.path.join(DATA_DIR, "submissions.json")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
ALLOWED_ORIGINS = {o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()}

WHOP_API_KEY = os.environ.get("WHOP_API_KEY", "")
WHOP_APP_ID = os.environ.get("WHOP_APP_ID", "")
DASHBOARD_FILE = "whop-dashboard.html"   # never in PUBLIC_FILES: only served after a paid-access check
EXP_RE = re.compile(r"^/experiences/(exp_[A-Za-z0-9]+)(?:/.*)?$")
USER_RE = re.compile(r"^user_[A-Za-z0-9]+$")
ACCESS_CACHE_SECONDS = 60
USER_RATE_LIMIT = 120    # paid-dashboard page loads per minute per user

MAX_BODY = 4096          # bytes
MAX_SUBMISSIONS = 20000
RATE_LIMIT = 30          # requests per minute per IP on POST routes
ASSETS = ["BTC", "ETH", "SOL", "XRP", "XLM"]

# Only these files may be served publicly. Everything else is 404.
PUBLIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/kimi-crypto-matrix-dash.html": "kimi-crypto-matrix-dash.html",
    "/analytica84-landing.html": "analytica84-landing.html",
    "/a84-bolt-logo.jpg": "a84-bolt-logo.jpg",
    "/dash-preview.png": "dash-preview.png",
}

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
HANDLE_RE = re.compile(r"^@?[A-Za-z0-9._]{1,40}$")

# --- Hyperliquid price feed (unchanged behavior) ---
_prices = {}
_lock = threading.Lock()


def _fetch_prices():
    while True:
        try:
            req = urllib.request.Request(
                "https://api.hyperliquid.xyz/info",
                data=json.dumps({"type": "allMids"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as res:
                mids = json.loads(res.read())
            with _lock:
                for asset in ASSETS:
                    if asset in mids:
                        _prices[asset] = float(mids[asset])
        except Exception as e:
            print("Price fetch error:", type(e).__name__)
        time.sleep(2)


# --- Submissions storage ---
_file_lock = threading.Lock()


def _load():
    try:
        with open(SUBMISSIONS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return []


def _save(data):
    tmp = SUBMISSIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SUBMISSIONS_FILE)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())


# --- Rate limiting ---
_hits = {}
_hits_lock = threading.Lock()


def _rate_ok(ip, limit=RATE_LIMIT):
    now = time.time()
    with _hits_lock:
        arr = [t for t in _hits.get(ip, []) if now - t < 60]
        if len(arr) >= limit:
            _hits[ip] = arr
            return False
        arr.append(now)
        _hits[ip] = arr
        if len(_hits) > 5000:
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60]:
                _hits.pop(k, None)
    return True


# --- Whop paid access ---
def _whop_enabled():
    return bool(WHOP_API_KEY and WHOP_APP_ID)


def _verify_user(token):
    """Return the Whop user id from a signed x-whop-user-token, or None."""
    try:
        from whop_sdk.lib.verify_user_token import try_verify_user_token
        payload = try_verify_user_token(token, app_id=WHOP_APP_ID)
        return payload.user_id if payload else None
    except Exception as e:
        print("Token verify error:", type(e).__name__)
        return None


_access_cache = {}
_access_lock = threading.Lock()


def _has_access(user_id, resource_id):
    """Ask Whop if this user has access (paid membership or team member). Fails closed."""
    key = (user_id, resource_id)
    now = time.time()
    with _access_lock:
        hit = _access_cache.get(key)
        if hit and now - hit < ACCESS_CACHE_SECONDS:
            return True
    try:
        req = urllib.request.Request(
            "https://api.whop.com/api/v1/users/%s/access/%s" % (user_id, resource_id),
            headers={"Authorization": "Bearer " + WHOP_API_KEY, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as res:
            data = json.loads(res.read())
        ok = data.get("has_access") is True
    except Exception as e:
        print("Whop access check error:", type(e).__name__)
        return False
    if ok:
        with _access_lock:
            if len(_access_cache) > 5000:
                _access_cache.clear()
            _access_cache[key] = now
    return ok


DENIED_HTML = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<title>ANALYTICA84</title></head>"
    "<body style='background:#000;color:#fff;font-family:Segoe UI,Tahoma,sans-serif;"
    "text-align:center;padding:60px 20px'>"
    "<h2 style='color:#00ffff'>ANALYTICA84</h2>"
    "<p>An active Analytica84 membership is required to view this dashboard.</p>"
    "<p style='color:#ff1493'>Open it from your Whop membership after purchase.</p>"
    "</body></html>"
)

UNAVAILABLE_HTML = DENIED_HTML.replace(
    "An active Analytica84 membership is required to view this dashboard.",
    "This dashboard is not available right now.",
).replace("Open it from your Whop membership after purchase.", "Please try again later.")


class Handler(BaseHTTPRequestHandler):
    server_version = "A84"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass  # avoid logging request data

    # --- helpers ---
    def _ip(self):
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[0].strip() if fwd else self.client_address[0]

    def _headers(self, status, ctype="application/json", extra=None, framable=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Content-Type-Options", "nosniff")
        if not framable:
            self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        origin = self.headers.get("Origin", "")
        if origin and origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        for k, v in (extra or {}).items():
            self.send_header(k, v)

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self._headers(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status, body, framable=False):
        data = body.encode() if isinstance(body, str) else body
        self._headers(status, "text/html; charset=utf-8", framable=framable)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _experience(self, exp_id):
        """Paid dashboard. Opens only inside Whop for a verified user with access."""
        if not _whop_enabled():
            return self._html(503, UNAVAILABLE_HTML, framable=True)
        token = self.headers.get("x-whop-user-token", "")
        user_id = _verify_user(token) if token else None
        if not user_id or not USER_RE.match(user_id):
            return self._html(403, DENIED_HTML, framable=True)
        if not _rate_ok("u:" + user_id, USER_RATE_LIMIT):
            return self._html(429, UNAVAILABLE_HTML, framable=True)
        if not _has_access(user_id, exp_id):
            return self._html(403, DENIED_HTML, framable=True)
        try:
            with open(os.path.join(BASE_DIR, DASHBOARD_FILE), "rb") as f:
                body = f.read()
        except OSError:
            return self._html(503, UNAVAILABLE_HTML, framable=True)
        return self._html(200, body, framable=True)

    def _is_admin(self):
        if not ADMIN_TOKEN:
            return False
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return hmac.compare_digest(auth[7:].encode(), ADMIN_TOKEN.encode())

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None, "bad request"
        if length <= 0 or length > MAX_BODY:
            return None, "bad request size"
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            return None, "bad json"
        if not isinstance(data, dict):
            return None, "bad json"
        return data, None

    def _path(self):
        return self.path.split("?", 1)[0]

    # --- methods ---
    def do_OPTIONS(self):
        self._headers(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self._path()

        if path == "/api/prices":
            with _lock:
                prices = dict(_prices)
            return self._json(200, prices)

        if path == "/api/stats":
            # counts only, no personal data
            subs = _load()
            return self._json(200, {
                "total": len(subs),
                "approved": sum(1 for s in subs if s.get("approved")),
            })

        if path in ("/api/submissions", "/api/pending", "/api/community"):
            if not self._is_admin():
                return self._json(401, {"error": "unauthorized"})
            subs = _load()
            if path == "/api/submissions":
                return self._json(200, {"count": len(subs), "submissions": subs})
            if path == "/api/pending":
                pending = [s for s in subs if s.get("wallet") and not s.get("approved")]
                return self._json(200, {"count": len(pending), "pending": pending})
            members = [{
                "handle": s.get("tiktok", ""),
                "joined": s.get("timestamp", ""),
                "status": "verified" if s.get("approved") else "pending",
                "has_wallet": bool(s.get("wallet")),
            } for s in subs]
            return self._json(200, {"count": len(members), "members": members})

        m = EXP_RE.match(path)
        if m:
            return self._experience(m.group(1))

        if path.startswith("/api/"):
            return self._json(404, {"error": "not found"})

        # static: whitelist only
        name = PUBLIC_FILES.get(path)
        if not name:
            return self._json(404, {"error": "not found"})
        full = os.path.join(BASE_DIR, name)
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            return self._json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        self._headers(200, ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self._path()
        if path not in ("/api/contact", "/api/verify-wallet", "/api/approve"):
            return self._json(404, {"error": "not found"})

        if path == "/api/approve":
            if not self._is_admin():
                return self._json(401, {"error": "unauthorized"})
        elif not _rate_ok(self._ip()):
            return self._json(429, {"error": "too many requests"})

        data, err = self._read_json()
        if err:
            return self._json(400, {"error": err})

        email = data.get("email")
        if not isinstance(email, str) or not EMAIL_RE.match(email):
            return self._json(400, {"error": "invalid email"})
        email = email.strip().lower()

        try:
            with _file_lock:
                subs = _load()

                if path == "/api/contact":
                    tiktok = data.get("tiktok", "")
                    if tiktok and (not isinstance(tiktok, str) or not HANDLE_RE.match(tiktok)):
                        return self._json(400, {"error": "invalid handle"})
                    if any(s.get("email") == email for s in subs):
                        return self._json(200, {"status": "saved", "next_step": "verify-wallet"})
                    if len(subs) >= MAX_SUBMISSIONS:
                        return self._json(503, {"error": "unavailable"})
                    subs.append({
                        "email": email,
                        "tiktok": tiktok,
                        "timestamp": _now(),
                        "approved": False,
                        "wallet": "",
                    })
                    _save(subs)
                    return self._json(200, {"status": "saved", "next_step": "verify-wallet"})

                if path == "/api/verify-wallet":
                    wallet = data.get("wallet")
                    if not isinstance(wallet, str) or not WALLET_RE.match(wallet):
                        return self._json(400, {"error": "invalid wallet"})
                    for s in subs:
                        if s.get("email") == email and not s.get("approved"):
                            s["wallet"] = wallet
                            s["wallet_submitted"] = _now()
                            break
                    _save(subs)
                    # same reply whether or not the email exists (no email probing)
                    return self._json(200, {"status": "wallet_saved", "message": "Waiting for approval"})

                # /api/approve (admin only)
                found = False
                for s in subs:
                    if s.get("email") == email:
                        s["approved"] = True
                        s["approved_at"] = _now()
                        found = True
                        break
                if not found:
                    return self._json(404, {"error": "not found"})
                _save(subs)
                return self._json(200, {"status": "approved"})
        except Exception as e:
            print("Server error:", type(e).__name__)
            return self._json(500, {"error": "server error"})


if __name__ == "__main__":
    threading.Thread(target=_fetch_prices, daemon=True).start()
    port = int(os.environ.get("PORT", 8000))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"A84 server running on port {port}")
    srv.serve_forever()
