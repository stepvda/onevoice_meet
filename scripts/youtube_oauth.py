#!/usr/bin/env python3
"""
One-time YouTube OAuth consent flow — produces a long-lived refresh_token
that meet's `meeting-api` will exchange for short-lived access tokens on
every upload.

USAGE
-----
1. Make sure your OAuth client (Desktop type) has http://localhost:8765
   listed as an authorized redirect URI. Desktop clients auto-allow
   loopback IPs; you only need to add it explicitly if the client type
   is "Web application".

2. Run on your LAPTOP (not the server):

       python3 scripts/youtube_oauth.py \\
         --client-id 477485434472-56rer4me6geuf3mf0gvum0fhh0lamghb.apps.googleusercontent.com \\
         --client-secret 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

3. Your browser opens. Sign in as the YouTube channel owner. Approve the
   "Manage your YouTube account" / upload scope.

4. The browser redirects to http://localhost:8765/?code=… and this script
   completes the exchange and prints:

       YOUTUBE_REFRESH_TOKEN=1//0g…

5. Paste that line into /opt/meet/.env on turn.witysk.org and restart
   meeting-api:

       ssh root@turn.witysk.org '
         echo "YOUTUBE_REFRESH_TOKEN=…" >> /opt/meet/.env
         cd /opt/meet && docker compose -p meet up -d --force-recreate meeting-api
       '

Requires: Python 3.11+, no third-party packages (urllib + http.server only).

The script never sees your Google password — only an OAuth authorization
code that is already scoped to the upload-only permission you just approved.
"""
from __future__ import annotations

import argparse
import http.server
import json
import secrets
import socketserver
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
REDIRECT_HOST = "localhost"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/"


class CodeCatcher(http.server.BaseHTTPRequestHandler):
    captured: dict = {}

    def log_message(self, format, *args):  # silence default access log
        return

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        CodeCatcher.captured.update(params)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if "code" in params:
            self.wfile.write(
                b"<!doctype html><meta charset=utf-8><title>YouTube OAuth</title>"
                b"<body style='font-family:sans-serif;padding:40px;background:#0E1E33;color:#fff'>"
                b"<h2 style='color:#4CAF50'>OAuth complete</h2>"
                b"<p>You can close this tab and return to the terminal.</p></body>"
            )
        else:
            err = params.get("error", "unknown")
            self.wfile.write(
                f"<!doctype html><body style='padding:40px;font-family:sans-serif'>"
                f"<h2>OAuth failed</h2><pre>{err}</pre>".encode()
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--port", type=int, default=REDIRECT_PORT)
    args = parser.parse_args()

    state = secrets.token_urlsafe(16)
    auth_qs = urllib.parse.urlencode({
        "client_id": args.client_id,
        "redirect_uri": f"http://{REDIRECT_HOST}:{args.port}/",
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        # `prompt=consent` forces Google to issue a refresh_token even on
        # repeat consent, which it otherwise withholds.
        "prompt": "consent",
        "state": state,
    })
    auth_url = f"{AUTH_URL}?{auth_qs}"

    server = socketserver.TCPServer(("", args.port), CodeCatcher)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    print(f"Opening browser for consent…\n  {auth_url}\n")
    webbrowser.open(auth_url)
    print(f"Listening on http://{REDIRECT_HOST}:{args.port} for the redirect.")
    print("(If the browser didn't open, copy the URL above and paste into a browser window.)\n")

    # Wait until we have a code (or the user kills with Ctrl-C).
    try:
        while "code" not in CodeCatcher.captured and "error" not in CodeCatcher.captured:
            server_thread.join(timeout=0.2)
    except KeyboardInterrupt:
        server.shutdown()
        print("\nAborted.")
        return 130
    finally:
        server.shutdown()

    if CodeCatcher.captured.get("state") != state:
        print(f"State mismatch (CSRF check failed): {CodeCatcher.captured}")
        return 2

    if "error" in CodeCatcher.captured:
        print(f"OAuth error: {CodeCatcher.captured['error']}")
        return 3

    code = CodeCatcher.captured["code"]
    print("Authorization code received; exchanging for tokens…")

    body = urllib.parse.urlencode({
        "code": code,
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "redirect_uri": f"http://{REDIRECT_HOST}:{args.port}/",
        "grant_type": "authorization_code",
    }).encode()

    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            tokens = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"Token exchange HTTP {e.code}: {e.read().decode()[:400]}")
        return 4

    refresh = tokens.get("refresh_token")
    access = tokens.get("access_token")
    if not refresh:
        print("No refresh_token returned. This usually means you previously")
        print("granted consent for the same client + scope; revoke it at")
        print("https://myaccount.google.com/permissions and try again.")
        print(f"Raw response: {tokens}")
        return 5

    print()
    print("=" * 70)
    print(" SUCCESS — paste the line below into /opt/meet/.env on the server")
    print("=" * 70)
    print()
    print(f"YOUTUBE_REFRESH_TOKEN={refresh}")
    print()
    print("(short-lived access_token preview:", access[:24] + "…)" if access else "")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
