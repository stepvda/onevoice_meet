#!/usr/bin/env python3
"""Reference client for meetpp-speech (stdlib only), signing requests exactly
as meetpp-agent must (contract section 6.2):

    X-Meetpp-Timestamp: <unix seconds>
    X-Meetpp-Signature: hex(HMAC_SHA256(secret, "v2\n" + ts + "\n" + METHOD + "\n"
                                                + target + "\n" + hex(sha256(raw_body))))

target = the path + "?" + query string exactly as sent (so language and prompt
are signed). A signature is accepted once: a retry must be signed again.

Usage:
    export MEETPP_SPEECH_URL=http://10.88.0.2:9310 MEETPP_SPEECH_SECRET=...
    python3 client_example.py health
    python3 client_example.py transcribe utterance.ogg --prompt "Meeting of ..."
    python3 client_example.py tts "Item three: the budget." -o clip.ogg
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def sign(secret: str, ts: int, method: str, target: str, body: bytes) -> str:
    msg = f"v2\n{ts}\n{method}\n{target}\n".encode() + hashlib.sha256(body).hexdigest().encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def _post(url: str, secret: str, body: bytes, content_type: str, timeout: float) -> tuple[int, dict, bytes]:
    ts = int(time.time())
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": content_type})
    # req.selector is the request target urllib puts on the wire (path + query).
    req.add_header("X-Meetpp-Timestamp", str(ts))
    req.add_header("X-Meetpp-Signature", sign(secret, ts, "POST", req.selector, body))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def transcribe(base: str, secret: str, audio: bytes, content_type: str = "audio/ogg",
               language: str = "en", prompt: str | None = None, timeout: float = 30.0) -> tuple[int, dict]:
    query = {"language": language}
    if prompt:
        query["prompt"] = prompt[-600:]
    url = f"{base.rstrip('/')}/transcribe?{urllib.parse.urlencode(query)}"
    status, _, body = _post(url, secret, audio, content_type, timeout)
    return status, json.loads(body or b"{}")


def tts(base: str, secret: str, text: str, voice: str = "am_michael", fmt: str = "ogg",
        timeout: float = 30.0) -> tuple[int, str, bytes]:
    body = json.dumps({"text": text, "voice": voice, "format": fmt}).encode()
    status, headers, data = _post(f"{base.rstrip('/')}/tts", secret, body, "application/json", timeout)
    ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    return status, ctype, data


def health(base: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(f"{base.rstrip('/')}/health", timeout=timeout) as resp:
        return json.loads(resp.read())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("MEETPP_SPEECH_URL", "http://127.0.0.1:9310"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    t = sub.add_parser("transcribe")
    t.add_argument("file")
    t.add_argument("--prompt")
    t.add_argument("--language", default="en")
    s = sub.add_parser("tts")
    s.add_argument("text")
    s.add_argument("--voice", default="am_michael")
    s.add_argument("--format", default="ogg", choices=["ogg", "wav"])
    s.add_argument("-o", "--out", default="tts.ogg")
    a = p.parse_args()

    if a.cmd == "health":
        print(json.dumps(health(a.url), indent=2))
        return 0
    secret = os.environ.get("MEETPP_SPEECH_SECRET")
    if not secret:
        print("set MEETPP_SPEECH_SECRET", file=sys.stderr)
        return 2
    if a.cmd == "transcribe":
        with open(a.file, "rb") as f:
            audio = f.read()
        ctype = "audio/wav" if a.file.lower().endswith(".wav") else "audio/ogg"
        status, out = transcribe(a.url, secret, audio, ctype, a.language, a.prompt)
        print(status, json.dumps(out, indent=2))
        return 0 if status == 200 else 1
    status, ctype, data = tts(a.url, secret, a.text, a.voice, a.format)
    if status != 200:
        print(status, data.decode(errors="replace"), file=sys.stderr)
        return 1
    with open(a.out, "wb") as f:
        f.write(data)
    print(f"{status} {ctype} {len(data)} bytes -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
