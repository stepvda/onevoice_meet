FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OMP_NUM_THREADS=2 \
    CT2_NUM_THREADS=2

# espeak-ng is needed by Piper for phonemisation; libsndfile for soundfile.
RUN apt-get update && apt-get install -y --no-install-recommends \
        espeak-ng libsndfile1 curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Bake the STT models at build time so first use is instant. Failures are
# tolerated (the agent degrades to no-op STT rather than failing to start).
RUN python -c "from faster_whisper import WhisperModel; [WhisperModel(n, device='cpu', compute_type='int8', download_root='/models/whisper') for n in ('small','base')]" \
    || echo "whisper model download failed; will retry at runtime"

# Silero VAD model. Best-effort: if the download fails the agent falls back to
# an energy-based gate.
RUN mkdir -p /models && \
    (curl -fsSL -o /models/silero_vad.onnx \
      https://github.com/snakers4/silero-vad/raw/master/files/silero_vad.onnx || true)

COPY agent ./agent

EXPOSE 8091

CMD ["uvicorn", "agent.main:app", "--host", "0.0.0.0", "--port", "8091"]
