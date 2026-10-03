"""Shared Meet++ helpers (single source of truth for person identity)."""
from __future__ import annotations

import re


def person_key(identity: str | None, name: str | None) -> str:
    """Canonical attendee key. Authenticated `user-<sub>` identities merge by
    sub; everyone else merges by normalised display name within the session.

    Used by both presence ingest (runtime) and `attendance.require_next`
    (ops) so they cannot derive different keys for the same person and create
    duplicate attendee rows.
    """
    if identity and identity.startswith("user-"):
        return identity
    normalised = re.sub(r"\s+", " ", (name or "").strip().lower())[:200]
    return normalised or (identity or "")
