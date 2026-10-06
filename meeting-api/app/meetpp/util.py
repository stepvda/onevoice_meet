"""Shared Meet++ helpers: identity, time, JSON and text similarity."""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from ulid import ULID

# LiveKit identities that never count as meeting participants.
SYSTEM_IDENTITY_PREFIXES = (
    "meetpp-",
    "playback",
    "composite-",
    "viewer-",
    "egress-",
    "ingress-",
    "EG_",
)

_GUEST_KEY_RE = re.compile(r"^guest:[A-Za-z0-9_-]{8,64}$")


def ulid() -> str:
    return str(ULID())


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime | None) -> str | None:
    dt = aware(dt)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return aware(value)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return aware(dt)


def loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        out = json.loads(value)
    except (TypeError, ValueError):
        return default
    if default is not None and not isinstance(out, type(default)):
        return default
    return out


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def is_system_identity(identity: str | None) -> bool:
    return not identity or identity.startswith(SYSTEM_IDENTITY_PREFIXES)


def sub_from_identity(identity: str | None) -> str | None:
    if identity and identity.startswith("user-") and len(identity) > 5:
        return identity[5:]
    return None


def person_key_for_identity(identity: str | None) -> str | None:
    """`user-<sub>` → `sub:<sub>`. Anonymous identities have no intrinsic key
    (guests send their `guest:<uuid>` key with the consent post)."""
    sub = sub_from_identity(identity)
    return f"sub:{sub}" if sub else None


def valid_guest_key(key: str | None) -> bool:
    return bool(key and _GUEST_KEY_RE.match(key))


def name_key(name: str) -> str:
    """Person key for someone known only by name (mentioned in speech)."""
    return "name:" + norm_name(name)[:180]


def norm_name(name: str | None) -> str:
    text = unicodedata.normalize("NFKD", (name or "").strip().lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s-]", " ", text)).strip()


def tokens(text: str | None) -> set[str]:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return {t for t in re.findall(r"\w+", text) if len(t) > 2}


def jaccard(a: str | set[str], b: str | set[str]) -> float:
    ta = a if isinstance(a, set) else tokens(a)
    tb = b if isinstance(b, set) else tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def truncate(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def word_count(text: str | None) -> int:
    return len(re.findall(r"\w+", text or ""))


_NORM_TRANS = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "—": "-", "–": "-", "*": " ", "_": " ", "#": " ", ">": " "})


def normalize_text(text: str | None) -> str:
    """Lower case, typographic quotes and dashes folded, Markdown marks and
    runs of whitespace removed: for checking that a quote is in a text."""
    return re.sub(r"\s+", " ", (text or "").translate(_NORM_TRANS).lower()).strip().strip(".")
