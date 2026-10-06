"""Room-metadata writes are serialised per room across event loops and threads
(APScheduler jobs run `asyncio.run()` in worker threads and reach
`patch_room_metadata` through reconcile_egress)."""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

from app.room_metadata import patch_room_metadata


class SlowLK:
    """LiveKitAPI.room stand-in that holds between read and write, so that
    unserialised read-modify-writes from two threads lose updates."""

    def __init__(self) -> None:
        self.metadata = "{}"
        self.room = self

    async def list_rooms(self, req):
        await asyncio.sleep(0.005)
        return SimpleNamespace(rooms=[SimpleNamespace(metadata=self.metadata)])

    async def update_room_metadata(self, req):
        await asyncio.sleep(0.005)
        self.metadata = req.metadata

    @property
    def md(self) -> dict:
        return json.loads(self.metadata)


def test_patches_from_two_event_loops_in_two_threads():
    lk = SlowLK()
    errors: list[BaseException] = []

    def run(prefix: str) -> None:
        async def patches() -> None:
            await asyncio.gather(*(
                patch_room_metadata(lk, "room-threads", lambda md, k=f"{prefix}{i}": md.update({k: True}))
                for i in range(5)
            ))

        try:
            asyncio.run(patches())
        except BaseException as exc:  # noqa: BLE001 — surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(p,)) for p in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors
    assert lk.md == {f"{p}{i}": True for p in ("a", "b") for i in range(5)}


async def test_a_cancelled_waiter_does_not_keep_the_lock():
    lk = SlowLK()
    first = asyncio.create_task(patch_room_metadata(lk, "room-cancel", lambda md: md.update(a=1)))
    await asyncio.sleep(0.001)
    waiter = asyncio.create_task(patch_room_metadata(lk, "room-cancel", lambda md: md.update(b=1)))
    await asyncio.sleep(0.001)
    waiter.cancel()
    await first
    await asyncio.wait_for(patch_room_metadata(lk, "room-cancel", lambda md: md.update(c=1)), timeout=2)
    assert lk.md == {"a": 1, "c": 1}
