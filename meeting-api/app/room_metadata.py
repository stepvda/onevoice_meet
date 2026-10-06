"""LiveKit room metadata: one read-modify-write at a time per room.

The room metadata is a single JSON document that several writers update
(layout and presenter, recording and streaming flags, PiP, Meet++, the stage
hand-over on screen shares). LiveKit only offers "replace the whole
document", so two concurrent read-modify-writes lose one update. Every
writer goes through `patch_room_metadata`, which serialises them per room
(meeting-api runs as a single process). The lock is a thread lock, not an
asyncio.Lock: scheduler jobs write from their own threads and event loops
(`asyncio.run()`), and an asyncio.Lock is bound to the first loop that waits
on it.
"""
from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from livekit import api

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


@asynccontextmanager
async def _room_lock(room_name: str) -> AsyncIterator[None]:
    with _locks_guard:
        lock = _locks.setdefault(room_name, threading.Lock())
    # Polled rather than acquired in a worker thread: a waiter cancelled
    # there would still take the lock and never release it.
    while not lock.acquire(blocking=False):
        await asyncio.sleep(0.01)
    try:
        yield
    finally:
        lock.release()


async def patch_room_metadata(
    lk: api.LiveKitAPI,
    room_name: str,
    change: Callable[[dict], bool | None],
    *,
    require_room: bool = False,
) -> dict | None:
    """Apply `change` to the current metadata (mutating the dict) and write it
    back. `change` returns False to skip the write. Returns the resulting
    metadata, or None when `require_room` is set and the room is not running
    (nothing written)."""
    async with _room_lock(room_name):
        rooms = await lk.room.list_rooms(api.ListRoomsRequest(names=[room_name]))
        if not rooms.rooms and require_room:
            return None
        current: dict = {}
        if rooms.rooms:
            try:
                parsed = json.loads(rooms.rooms[0].metadata or "{}")
                current = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                current = {}
        if change(current) is False:
            return current
        await lk.room.update_room_metadata(
            api.UpdateRoomMetadataRequest(room=room_name, metadata=json.dumps(current))
        )
        return current
