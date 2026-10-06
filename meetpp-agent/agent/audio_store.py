"""Per-utterance audio store for tier 2 (contract §6.1, FDD §7.4).

``$MEETPP_DATA_DIR/<sid>/audio/<utterance_id>.ogg`` (Ogg/Opus, 24 kbit/s,
mono, 16 kHz input) plus ``<sid>/audio/index.jsonl`` with one line per
utterance ``{utterance_id, identity, name, t_start, t_end, duration_s, text}``.
meeting-api deletes ``<sid>/audio/`` at publish and after the retention
period. Writing is refused while free disk space is below 3 GB; the session
then reports tier 2 as "down".
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Callable

import numpy as np

from . import config

log = logging.getLogger("meetpp.agent")

OPUS_BITRATE = 24000


def encode_opus(audio: np.ndarray, sample_rate: int = config.SAMPLE_RATE, bitrate: int = OPUS_BITRATE) -> bytes:
    import av

    pcm = (np.clip(audio.astype(np.float32, copy=False), -1.0, 1.0) * 32767.0).astype(np.int16).reshape(1, -1)
    buf = io.BytesIO()
    container = av.open(buf, mode="w", format="ogg")
    try:
        stream = container.add_stream("libopus", rate=sample_rate)
        stream.bit_rate = bitrate
        stream.layout = "mono"
        frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
        frame.sample_rate = sample_rate
        frame.pts = 0
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    finally:
        container.close()
    return buf.getvalue()


def decode_audio(data: bytes, sample_rate: int = config.SAMPLE_RATE) -> np.ndarray:
    """Decode any container PyAV understands to float32 mono at ``sample_rate``."""
    import av

    out = []
    with av.open(io.BytesIO(data)) as container:
        resampler = av.AudioResampler(format="flt", layout="mono", rate=sample_rate)
        for frame in container.decode(audio=0):
            for rf in resampler.resample(frame):
                out.append(rf.to_ndarray().reshape(-1))
        for rf in resampler.resample(None):
            out.append(rf.to_ndarray().reshape(-1))
    return np.concatenate(out).astype(np.float32) if out else np.zeros(0, dtype=np.float32)


class AudioStore:
    def __init__(
        self,
        data_dir: Path | str,
        sid: str,
        *,
        min_free_bytes: int = config.MIN_FREE_BYTES,
        disk_usage: Callable[[str], object] = shutil.disk_usage,
    ) -> None:
        self.dir = Path(data_dir) / sid / "audio"
        self.index_path = self.dir / "index.jsonl"
        self.min_free_bytes = min_free_bytes
        self._disk_usage = disk_usage
        self._lock = threading.Lock()
        self.ok = True  # False while the disk guard refuses writes
        self.written = 0
        self.refused = 0
        self.errors = 0

    def _free_bytes(self) -> int:
        probe = self.dir
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return int(self._disk_usage(str(probe)).free)

    def disk_ok(self) -> bool:
        try:
            free = self._free_bytes()
        except OSError as exc:
            log.warning("MEETPP_AUDIO cannot stat %s: %s", self.dir, exc)
            return False
        good = free >= self.min_free_bytes
        if good != self.ok:
            if good:
                log.info("MEETPP_AUDIO free disk %.1f GB: audio store resumed (%s)", free / 1e9, self.dir)
            else:
                log.warning(
                    "MEETPP_AUDIO free disk %.2f GB < %.1f GB: refusing to store audio; tier 2 reported down (%s)",
                    free / 1e9,
                    self.min_free_bytes / 1e9,
                    self.dir,
                )
        self.ok = good
        return good

    def write(
        self,
        utterance_id: str,
        audio: np.ndarray,
        *,
        identity: str,
        name: str,
        t_start: str,
        t_end: str,
        text: str = "",
    ) -> bytes | None:
        """Encode and store one utterance. Returns the Ogg/Opus bytes, or None
        when refused (disk guard) or failed. Blocking: call off the loop."""
        if not self.disk_ok():
            self.refused += 1
            return None
        try:
            data = encode_opus(audio)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            log.warning("MEETPP_AUDIO encode failed utterance=%s: %s", utterance_id, exc)
            return None
        entry = {
            "utterance_id": utterance_id,
            "identity": identity,
            "name": name,
            "t_start": t_start,
            "t_end": t_end,
            "duration_s": round(len(audio) / config.SAMPLE_RATE, 3),
            "text": text,
        }
        try:
            with self._lock:
                self.dir.mkdir(parents=True, exist_ok=True)
                path = self.dir / f"{utterance_id}.ogg"
                tmp = path.with_suffix(".ogg.tmp")
                tmp.write_bytes(data)
                os.replace(tmp, path)
                with open(self.index_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    fh.flush()
        except OSError as exc:
            self.errors += 1
            log.warning("MEETPP_AUDIO write failed utterance=%s: %s", utterance_id, exc)
            return None
        self.written += 1
        return data

    def read(self, utterance_id: str) -> bytes | None:
        try:
            return (self.dir / f"{utterance_id}.ogg").read_bytes()
        except OSError:
            return None

    def entries(self) -> list[dict]:
        """All indexed utterances (deduplicated) in time order."""
        rows: dict[str, dict] = {}
        try:
            with open(self.index_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("utterance_id"):
                        rows[row["utterance_id"]] = row
        except FileNotFoundError:
            return []
        return sorted(rows.values(), key=lambda r: (r.get("t_start") or "", r.get("utterance_id")))
