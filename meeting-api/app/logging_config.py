"""
Centralised logging configuration.

Three log files, all rotated daily at midnight, gzipped on rotation, and
kept for 6 months (180 days):

  /var/log/meet/app.log       Everything from `logging.getLogger(__name__)`
                              calls and uvicorn's "app" loggers — general
                              application events, errors, warnings.
  /var/log/meet/requests.log  One line per HTTP request (method, path,
                              status, duration, client IP, user-agent).
                              Written by the middleware in `main.py`.
  /var/log/meet/db.log        SQLAlchemy engine logs — emitted SQL,
                              connection lifecycle, ROLLBACKs. Default
                              level INFO so SELECT queries don't drown the
                              file; bump to DEBUG via LOG_DB_LEVEL to see
                              every statement.

`GzipTimedRotatingFileHandler` is the stock `TimedRotatingFileHandler`
that also gzips the just-rotated file after each midnight rollover.
"""
from __future__ import annotations

import glob
import gzip
import logging
import os
import shutil
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any


LOG_DIR = Path(os.environ.get("LOG_DIR", "/var/log/meet"))
BACKUP_DAYS = int(os.environ.get("LOG_BACKUP_DAYS", "180"))  # ~6 months


class GzipTimedRotatingFileHandler(TimedRotatingFileHandler):
    """TimedRotatingFileHandler that gzips rotated files in-place.

    Stock behavior keeps rotated logs as `<base>.YYYY-MM-DD` — we walk the
    base directory after each rollover and gzip any matching file that
    isn't already compressed. Compressed files keep the `.gz` suffix so
    `backupCount` rounding still does the right thing.
    """

    def doRollover(self) -> None:  # noqa: D401
        super().doRollover()
        base_dir = os.path.dirname(self.baseFilename) or "."
        base_name = os.path.basename(self.baseFilename)
        # `TimedRotatingFileHandler` writes rotated files named like
        # `<base>.YYYY-MM-DD` (no extension change). Anything matching the
        # base prefix with a suffix is a candidate.
        for path in glob.glob(os.path.join(base_dir, base_name + ".*")):
            if path.endswith(".gz") or path == self.baseFilename:
                continue
            try:
                with open(path, "rb") as src, gzip.open(path + ".gz", "wb", compresslevel=6) as dst:
                    shutil.copyfileobj(src, dst)
                os.remove(path)
            except OSError as e:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "log_gzip_failed path=%s err=%s", path, e,
                )


def _make_handler(filename: str, level: int = logging.INFO) -> logging.Handler:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    h = GzipTimedRotatingFileHandler(
        filename=str(LOG_DIR / filename),
        when="midnight",
        interval=1,
        backupCount=BACKUP_DAYS,
        encoding="utf-8",
        utc=False,
        delay=False,
    )
    h.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    fmt.converter = time.localtime
    h.setFormatter(fmt)
    return h


# Module-level singletons set up once during `setup_logging()`.
_app_handler: logging.Handler | None = None
_requests_handler: logging.Handler | None = None
_db_handler: logging.Handler | None = None

# Logger names that should *not* propagate to the root file (we route them
# explicitly to their own file instead).
_DEDICATED_LOGGERS = ("requests", "sqlalchemy.engine")


def setup_logging() -> None:
    """Configure logging at app startup. Idempotent."""
    global _app_handler, _requests_handler, _db_handler

    if _app_handler is not None:
        return  # already configured

    _app_handler = _make_handler("app.log", level=logging.INFO)
    _requests_handler = _make_handler("requests.log", level=logging.INFO)
    _db_handler = _make_handler("db.log", level=logging.INFO)

    # Root logger → app.log + stdout (so `docker logs` still works).
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Replace any default handlers so we don't double-emit.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(_app_handler)
    stream = logging.StreamHandler()
    stream.setLevel(logging.INFO)
    stream.setFormatter(_app_handler.formatter)
    root.addHandler(stream)

    # Dedicated "requests" logger — only writes to requests.log.
    req_logger = logging.getLogger("requests")
    req_logger.setLevel(logging.INFO)
    req_logger.propagate = False
    for h in list(req_logger.handlers):
        req_logger.removeHandler(h)
    req_logger.addHandler(_requests_handler)

    # SQLAlchemy → db.log. Level controllable via env so verbose query
    # logging is opt-in.
    sa_level_name = os.environ.get("LOG_DB_LEVEL", "INFO").upper()
    sa_level = getattr(logging, sa_level_name, logging.INFO)
    sql_logger = logging.getLogger("sqlalchemy.engine")
    sql_logger.setLevel(sa_level)
    sql_logger.propagate = False
    for h in list(sql_logger.handlers):
        sql_logger.removeHandler(h)
    sql_logger.addHandler(_db_handler)

    # Bring uvicorn's loggers in line with our root config so they land in
    # app.log alongside everything else (without dropping stdout).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        # uvicorn.access duplicates what our middleware logs — silence it
        # to keep app.log focused on application events.
        if name == "uvicorn.access":
            lg.disabled = True

    logging.getLogger(__name__).info(
        "logging_configured app=%s requests=%s db=%s backup_days=%d",
        LOG_DIR / "app.log",
        LOG_DIR / "requests.log",
        LOG_DIR / "db.log",
        BACKUP_DAYS,
    )


def request_logger() -> logging.Logger:
    return logging.getLogger("requests")


def log_event(event: str, **fields: Any) -> None:
    """Helper for the request middleware: structured key=value line."""
    pieces = " ".join(f"{k}={v}" for k, v in fields.items())
    request_logger().info("%s %s", event, pieces)
