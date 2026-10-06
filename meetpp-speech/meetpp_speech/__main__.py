"""Entry point: python -m meetpp_speech (see run.sh)."""

from __future__ import annotations

import errno
import logging
import os
import socket
import sys
import time

import uvicorn

from .app import create_app
from .config import ConfigError, Settings

log = logging.getLogger("meetpp_speech")


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind before loading the models. If the WireGuard address is not up yet
    (boot order) keep retrying here instead of exiting, so launchd does not
    restart-loop the process through repeated model loads."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    attempt = 0
    while True:
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            sock.set_inheritable(True)
            return sock
        except OSError as exc:
            sock.close()
            if exc.errno not in (errno.EADDRNOTAVAIL, errno.EADDRINUSE):
                raise
            if attempt % 12 == 0:
                log.warning("cannot bind %s:%d yet (%s); retrying every 5s", host, port, exc.strerror)
            attempt += 1
            time.sleep(5)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("MEETPP_SPEECH_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # huggingface_hub progress bars would spam the launchd log
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    if len(settings.secret) < 32:
        log.warning("MEETPP_SPEECH_SECRET is short (%d chars); use 32+ random chars", len(settings.secret))
    if settings.bind not in ("127.0.0.1", "::1", "localhost"):
        log.info("listening on %s:%d (expected: the WireGuard address only)", settings.bind, settings.port)

    sock = bind_socket(settings.bind, settings.port)
    config = uvicorn.Config(
        create_app(settings),
        log_config=None,      # use the logging set up above
        access_log=False,     # own access log without query strings (prompt = meeting text)
        server_header=False,
        timeout_keep_alive=30,
        limit_concurrency=64,
    )
    server = uvicorn.Server(config)
    server.run(sockets=[sock])
    return 0


if __name__ == "__main__":
    sys.exit(main())
