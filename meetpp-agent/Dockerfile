FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OMP_NUM_THREADS=2

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Bake the tier-1 models (small + base, int8). No "|| true": the build fails
# if they cannot be fetched; the agent never runs without them.
RUN python -c "from faster_whisper import WhisperModel; [WhisperModel(n, device='cpu', compute_type='int8', download_root='/models/whisper') for n in ('small', 'base')]"

# Vendored Silero VAD. The build fails if its SHA-256 does not match
# agent/vad.py:SILERO_SHA256, and if PyAV cannot encode Ogg/Opus.
COPY models/silero_vad.onnx ./models/silero_vad.onnx
COPY agent ./agent
RUN python -m agent.vad --verify ./models/silero_vad.onnx && \
    python -c "import numpy as np; from agent.audio_store import encode_opus; assert encode_opus(np.zeros(16000, np.float32))[:4] == b'OggS'; print('opus ok')"

ENV WHISPER_MODEL_DIR=/models/whisper \
    SILERO_VAD_MODEL=/app/models/silero_vad.onnx \
    HF_HUB_OFFLINE=1 \
    MEETPP_DATA_DIR=/var/lib/meet/meetpp

EXPOSE 8091

CMD ["uvicorn", "agent.main:app", "--host", "0.0.0.0", "--port", "8091", "--no-access-log"]
