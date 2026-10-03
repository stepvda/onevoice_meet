"""Server-rendered strings (announcements, e-mail, PDF) in en/nl/fr/de,
falling back to en. Generated content uses the session language, not the
viewer's UI language."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent / "locales"
_AVAILABLE = ("en", "nl", "fr", "de")


@lru_cache(maxsize=8)
def _load(lang: str) -> dict:
    path = _DIR / f"{lang}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def t(lang: str, key: str, **vars) -> str:
    """Look up a dotted key in the session language, falling back to en and
    then the key itself. `{name}` placeholders are substituted."""
    if lang not in _AVAILABLE:
        lang = "en"
    value = _lookup(_load(lang), key) or _lookup(_load("en"), key) or key
    try:
        return value.format(**vars) if vars else value
    except (KeyError, IndexError):
        return value


def _lookup(data: dict, key: str):
    cur = data
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur if isinstance(cur, str) else None
