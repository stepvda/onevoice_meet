"""Who is on the stage: the room-wide presenter (FDD v3.2, Meet++ board as a
stream window; frontend twin: frontend/src/lib/stage.ts).

`presenter_identity` in the room metadata names a stream window:
  - a camera: the participant identity (the playback ingress is "playback");
  - a screen share: "<identity>#screen";
  - the Meet++ board: "meetpp:board".

The host and co-hosts set it (`POST /meetings/{id}/presenter`). The server
moves it on its own in three cases, and these functions are those moves
(each mutates the metadata dict and returns whether it changed it):
  - a Meet++ session starts: the board becomes the presenter, unless a screen
    share or the playback holds the stage: then the board follows it;
  - a screen share or the playback starts while a presenter is set: it takes
    the stage, and the previous presenter is kept in `presenter_prev`;
  - that stream stops: the most recent stream still live takes the stage,
    else the previous presenter comes back; a Meet++ session ending takes the
    board off the stage.
The live screen shares and playback are kept, oldest first, in
`stage_streams` (with or without a presenter).
"""
from __future__ import annotations

BOARD_KEY = "meetpp:board"
PLAYBACK_KEY = "playback"
STREAMS = "stage_streams"


def screen_key(identity: str) -> str:
    return f"{identity}#screen"


def is_stream(key: str | None) -> bool:
    """A screen share or the playback: content that takes the stage by itself."""
    return bool(key) and (key.endswith("#screen") or key == PLAYBACK_KEY)


def _live(md: dict) -> list[str]:
    streams = md.get(STREAMS)
    return [k for k in streams if isinstance(k, str)] if isinstance(streams, list) else []


def _set_live(md: dict, live: list[str]) -> None:
    if live:
        md[STREAMS] = live
    else:
        md.pop(STREAMS, None)


def stream_started(md: dict, key: str) -> bool:
    before = _live(md)
    _set_live(md, [k for k in before if k != key] + [key])
    changed = md[STREAMS] != before
    current = md.get("presenter_identity")
    if not current or current == key:
        # No presenter: the stage ladder already puts a screen share or the
        # playback first.
        return changed
    if not is_stream(current):
        md["presenter_prev"] = current
    md["presenter_identity"] = key
    return True


def stream_stopped(md: dict, key: str) -> bool:
    before = _live(md)
    live = [k for k in before if k != key]
    _set_live(md, live)
    current = md.get("presenter_identity")
    if current == key:
        md["presenter_identity"] = live[-1] if live else md.pop("presenter_prev", None)
        return True
    if not current and not live and md.get("presenter_prev"):
        # The last stream held the stage without a presenter (the board
        # followed it): the board comes back.
        md["presenter_identity"] = md.pop("presenter_prev")
        return True
    return live != before


def board_started(md: dict) -> bool:
    current = md.get("presenter_identity")
    if current == BOARD_KEY:
        return False
    if is_stream(current) or (not current and _live(md)):
        # A screen share or the playback holds the stage: it keeps it, the
        # board follows it.
        md["presenter_prev"] = BOARD_KEY
    else:
        md["presenter_identity"] = BOARD_KEY
        md.pop("presenter_prev", None)
    return True


def board_ended(md: dict) -> bool:
    changed = False
    if md.get("presenter_identity") == BOARD_KEY:
        md["presenter_identity"] = None
        changed = True
    if md.get("presenter_prev") == BOARD_KEY:
        md.pop("presenter_prev", None)
        changed = True
    return changed


def chosen(md: dict, key: str | None) -> bool:
    """The host chose the presenter: that choice stands, nothing comes back."""
    md["presenter_identity"] = key
    md.pop("presenter_prev", None)
    return True
