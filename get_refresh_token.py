#!/usr/bin/env python3
"""
Fetch Husqvarna Automower refresh token automatically.

- Normal mode: starts local server, opens browser, captures ?code=...
- Manual mode: pass --code XYZ on command line to skip browser
- Exchanges code -> tokens
- Prints refresh_token and updates .env
"""

import argparse
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

# Load .env
load_dotenv()

CLIENT_ID = os.environ.get("HUSQ_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("HUSQ_CLIENT_SECRET", "").strip()
REDIRECT_URI = os.environ.get("HUSQ_REDIRECT_URI", "http://localhost/callback").strip()
SCOPE = os.environ.get("HUSQ_SCOPE", "iam:read amc:read amc:control").strip().strip('"')

AUTH_URL = "https://api.authentication.husqvarnagroup.dev/v1/oauth2/authorize"
TOKEN_URL = "https://api.authentication.husqvarnagroup.dev/v1/oauth2/token"

STATE = secrets.token_urlsafe(16)
CODE_HOLDER = {"code": None, "error": None, "state": None}


def post_form(url: str, data: dict) -> dict:
    enc = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=enc, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        return

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != urllib.parse.urlparse(REDIRECT_URI).path:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return

        q = urllib.parse.parse_qs(parsed.query)
        CODE_HOLDER["code"] = (q.get("code") or [""])[0]
        CODE_HOLDER["error"] = (q.get("error") or [""])[0]
        CODE_HOLDER["state"] = (q.get("state") or [""])[0]

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK, got code. You can close this window.")
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def update_dotenv(refresh_token: str):
    env_path = Path(".env")
    if not env_path.exists():
        env_path.write_text(f"HUSQ_REFRESH_TOKEN={refresh_token}\n", encoding="utf-8")
        return
    lines = env_path.read_text(encoding="utf-8").splitlines()
    wrote = False
    for i, line in enumerate(lines):
        if line.strip().startswith("HUSQ_REFRESH_TOKEN="):
            lines[i] = f"HUSQ_REFRESH_TOKEN={refresh_token}"
            wrote = True
            break
    if not wrote:
        lines.append(f"HUSQ_REFRESH_TOKEN={refresh_token}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def exchange_code(code: str):
    token = post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "redirect_uri": REDIRECT_URI,
    })
    refresh = token.get("refresh_token")
    access = token.get("access_token")
    print("\n✅ SUCCESS")
    print("refresh_token:", refresh)
    print("access_token :", (access[:40] + "...") if access else None)
    if refresh:
        update_dotenv(refresh)
        print("Updated .env with HUSQ_REFRESH_TOKEN.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", help="Manually provide authorization code instead of browser flow")
    args = parser.parse_args()

    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: set HUSQ_CLIENT_ID and HUSQ_CLIENT_SECRET in .env")
        sys.exit(1)

    if args.code:
        exchange_code(args.code)
        return

    # Start local server
    ru = urllib.parse.urlparse(REDIRECT_URI)
    port = ru.port or 80
    server = http.server.HTTPServer(("localhost", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Listening on {REDIRECT_URI}")

    # Open browser
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": STATE,
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    print("\nOpen this URL if it doesn't open automatically:\n", url, "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    # Wait until code arrives
    while CODE_HOLDER["code"] is None and CODE_HOLDER["error"] is None:
        pass

    if CODE_HOLDER["error"]:
        print("OAuth error:", CODE_HOLDER["error"])
        return
    if CODE_HOLDER["state"] != STATE:
        print("ERROR: state mismatch")
        return

    exchange_code(CODE_HOLDER["code"])


if __name__ == "__main__":
    main()
