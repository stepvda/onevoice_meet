from __future__ import annotations

import asyncio
import logging

import numpy as np

from agent.consumer import SampleClock, TrackConsumer
from tests.fakes import EnergyGateVAD, silence, sync_offload, tone


class FlakyStreams:
    """Stream factory: the first ``failures`` streams raise after a few frames;
    later streams yield ``audio`` (10 ms frames) and then idle."""

    def __init__(self, failures: int = 0, audio: np.ndarray | None = None, end_normally: int = 0) -> None:
        self.failures = failures
        self.end_normally = end_normally
        self.audio = audio if audio is not None else np.zeros(0, dtype=np.float32)
        self.opened = 0
        self.closed = 0

    def __call__(self, track):
        self.opened += 1
        n = self.opened
        return self._gen(n)

    async def _gen(self, n):
        try:
            if n <= self.failures:
                for _ in range(3):
                    await asyncio.sleep(0)
                    yield np.zeros(160, dtype=np.float32)
                raise RuntimeError(f"decoder exploded #{n}")
            if n <= self.failures + self.end_normally:
                yield np.zeros(160, dtype=np.float32)
                return
            for i in range(0, len(self.audio), 160):
                await asyncio.sleep(0)
                yield self.audio[i : i + 160]
            while True:
                await asyncio.sleep(0.005)
                yield np.zeros(160, dtype=np.float32)
        finally:
            self.closed += 1


def make(streams, **kw):
    utts, restarts = [], []
    c = TrackConsumer(
        track_sid="TR_1",
        identity="user-a",
        name="Ann",
        track=object(),
        stream_factory=streams,
        vad_factory=EnergyGateVAD,
        on_utterance=lambda c, u, t0, t1: utts.append((u, t0, t1)),
        on_restart=lambda c, reason, delay, at: restarts.append((reason, delay)),
        backoff=(0.01, 0.02, 0.05, 0.1),
        offload=sync_offload,
        **kw,
    )
    return c, utts, restarts


async def wait_for(cond, timeout=2.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("condition not met")
        await asyncio.sleep(0.005)


async def test_restarts_with_backoff_after_exceptions(caplog):
    streams = FlakyStreams(failures=3)
    c, _, restarts = make(streams)
    with caplog.at_level(logging.WARNING, logger="meetpp.agent"):
        c.start()
        await wait_for(lambda: streams.opened == 4 and c.alive)
    assert c.restarts == 3
    assert [d for _, d in restarts] == [0.01, 0.02, 0.05]
    assert all("RuntimeError" in r for r, _ in restarts)
    assert sum("MEETPP_CONSUMER restart" in r.message for r in caplog.records) == 3
    await c.stop()
    assert not c.running and streams.closed == 4  # every stream closed
    opened = streams.opened
    await asyncio.sleep(0.05)
    assert streams.opened == opened  # no restart after stop


async def test_backoff_caps_at_last_step():
    streams = FlakyStreams(failures=6)
    c, _, restarts = make(streams)
    c.start()
    await wait_for(lambda: streams.opened == 7 and c.alive, timeout=3)
    assert [d for _, d in restarts] == [0.01, 0.02, 0.05, 0.1, 0.1, 0.1]
    await c.stop()


async def test_stream_end_is_restarted():
    streams = FlakyStreams(end_normally=1)
    c, _, restarts = make(streams)
    c.start()
    await wait_for(lambda: streams.opened == 2 and c.alive)
    assert restarts[0][0] == "stream ended"
    await c.stop()


async def test_liveness_restart_is_immediate():
    streams = FlakyStreams()
    c, _, restarts = make(streams)
    c.start()
    await wait_for(lambda: c.alive)
    c.restart("liveness")
    await wait_for(lambda: streams.opened == 2 and c.alive)
    assert restarts == [("liveness", 0.0)]
    await c.stop()


async def test_emits_utterances_with_wall_times_and_flushes_on_stop():
    audio = np.concatenate([silence(16000), tone(16000), silence(16000), tone(12000)])
    streams = FlakyStreams(audio=audio)
    c, utts, _ = make(streams)
    c.start()
    await wait_for(lambda: len(utts) == 1)
    u, t0, t1 = utts[0]
    assert abs((t1 - t0) - u.duration_s) < 1e-6
    await wait_for(lambda: c.segmenter.in_speech and c.segmenter.samples_seen >= 60000)
    await c.stop(flush=True)  # the open second utterance is flushed, not lost
    assert len(utts) == 2


async def test_crash_keeps_speech_already_buffered():
    # first stream: speech then crash mid-utterance; the segmenter content
    # is flushed when the consumer restarts
    class CrashMidSpeech(FlakyStreams):
        async def _gen(self, n):
            try:
                if n == 1:
                    a = np.concatenate([silence(4000), tone(16000)])
                    for i in range(0, len(a), 160):
                        await asyncio.sleep(0)
                        yield a[i : i + 160]
                    raise OSError("track gone")
                while True:
                    await asyncio.sleep(0.005)
                    yield np.zeros(160, dtype=np.float32)
            finally:
                self.closed += 1

    streams = CrashMidSpeech()
    c, utts, _ = make(streams)
    c.start()
    await wait_for(lambda: streams.opened == 2)
    await wait_for(lambda: len(utts) == 1)
    await c.stop()


async def test_paused_frames_are_discarded():
    audio = np.concatenate([silence(8000), tone(16000), silence(16000)])
    paused = {"v": True}
    streams = FlakyStreams(audio=audio)
    c, utts, _ = make(streams, paused=lambda: paused["v"])
    c.start()
    await asyncio.sleep(0.2)
    assert utts == []
    await c.stop()


def test_sample_clock_reanchors_after_gaps():
    clk = SampleClock()
    clk.on_samples(160, 100.01)
    for i in range(2, 101):
        clk.on_samples(160, 100.0 + i * 0.01)
    assert abs(clk.wall(16000) - 101.0) < 1e-6
    clk.on_samples(160, 200.01)  # stream resumed 99 s later (muted track)
    assert abs(clk.wall(16000 + 160) - 200.01) < 1e-6
    assert abs(clk.wall(8000) - 100.5) < 1e-6  # older samples keep their anchor


async def test_stop_interrupts_backoff_wait():
    streams = FlakyStreams(failures=10)
    c, _, restarts = make(streams)
    c.backoff = (10.0,)
    c.start()
    await wait_for(lambda: len(restarts) == 1)  # now sleeping 10 s
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await c.stop()
    assert loop.time() - t0 < 1.0 and not c.running
