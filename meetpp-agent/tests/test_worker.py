from __future__ import annotations

import threading
import time

import numpy as np

from agent.worker import STTWorker, WorkItem
from tests.fakes import FakeEngine


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def item(name: str, results: list, drops: list, sid: str = "s1", seconds: float = 2.0) -> WorkItem:
    return WorkItem(
        session_id=sid,
        utterance_id=name,
        identity="user-a",
        name="A",
        audio=np.zeros(int(seconds * 16000), dtype=np.float32),
        t_start=1000.0,
        t_end=1000.0 + seconds,
        on_result=lambda it, res: results.append((it.utterance_id, res.model)),
        on_drop=lambda it, reason: drops.append((it.utterance_id, reason)),
    )


def test_backlog_is_age_of_oldest_unfinished():
    clock = Clock()
    w = STTWorker(FakeEngine(), clock=clock)
    res, drops = [], []
    assert w.backlog_s() == 0.0
    w.submit(item("a", res, drops))
    clock.t = 3.0
    w.submit(item("b", res, drops))
    clock.t = 7.5
    assert w.backlog_s() == 7.5
    it = w.next_item()  # a in flight still counts
    assert it.utterance_id == "a" and w.backlog_s() == 7.5
    w.process_one(it)
    assert w.backlog_s() == 4.5  # b, enqueued at 3.0
    assert w.backlog_s("other-session") == 0.0
    assert w.pending("s1") == 1


def test_degrade_to_base_above_20s_until_below_5s():
    clock = Clock()
    eng = FakeEngine()
    w = STTWorker(eng, clock=clock)
    res, drops = [], []
    w.submit(item("a", res, drops))  # t=0
    clock.t = 10.0
    w.submit(item("b", res, drops))
    clock.t = 18.0
    w.submit(item("c", res, drops))
    clock.t = 21.0
    w.process_one(w.next_item())  # backlog 21 → base
    assert w.degraded
    clock.t = 22.0
    w.process_one(w.next_item())  # backlog 12 → stays on base (hysteresis)
    clock.t = 24.0
    w.process_one(w.next_item())  # backlog 6 → still base
    w.submit(item("d", res, drops))
    clock.t = 26.0
    w.process_one(w.next_item())  # backlog 2 → back to small
    assert not w.degraded
    assert [m for _, m in res] == ["base", "base", "base", "small"]
    assert drops == []


def test_drop_oldest_over_60s_is_reported_never_silent():
    clock = Clock()
    w = STTWorker(FakeEngine(), clock=clock)
    res, drops = [], []
    w.submit(item("a", res, drops))
    clock.t = 30.0
    w.submit(item("b", res, drops))
    clock.t = 61.0
    w.submit(item("c", res, drops))
    assert drops == [("a", "overloaded")]
    assert w.pending() == 2 and w.dropped == 1
    clock.t = 95.0  # b now 65 s old → dropped when the worker picks the next item
    got = w.next_item()
    assert drops[-1] == ("b", "overloaded")
    assert got.utterance_id == "c"


def test_single_worker_thread_processes_fifo():
    eng = FakeEngine()
    names = []
    seen_threads = set()
    done = threading.Event()

    def on_result(it, res):
        names.append(it.utterance_id)
        seen_threads.add(threading.current_thread().name)
        if len(names) == 5:
            done.set()

    w = STTWorker(eng)  # dispatch inline → runs on the worker thread
    w.start()
    try:
        for i in range(5):
            it = item(f"u{i}", [], [])
            it.on_result = on_result
            w.submit(it)
        assert done.wait(5)
    finally:
        w.stop()
    assert names == [f"u{i}" for i in range(5)]
    assert seen_threads == {"stt-worker"}


def test_waits_for_model_then_processes():
    eng = FakeEngine()
    eng.ready.clear()
    got = threading.Event()
    w = STTWorker(eng)
    w.start()
    try:
        it = item("late", [], [])
        it.on_result = lambda *_: got.set()
        w.submit(it)
        time.sleep(0.3)
        assert not got.is_set()
        eng.ready.set()
        assert got.wait(3)
    finally:
        w.stop()


def test_prompt_is_evaluated_at_decode_time():
    eng = FakeEngine()
    w = STTWorker(eng)
    state = {"ctx": "old"}
    it = item("p", [], [])
    it.prompt_fn = lambda: state["ctx"]
    w.submit(it)
    state["ctx"] = "new context"
    w.process_one(w.next_item())
    assert eng.calls[-1] == ("small", "new context")
