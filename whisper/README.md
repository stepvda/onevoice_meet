# whisper

Self-hosted speech-to-text for completed recordings. A small Docker image that builds [`whisper.cpp`](https://github.com/ggerganov/whisper.cpp) from source and bakes in the `ggml-base.en` model (~145 MB, English-only).

## What it provides

An OpenAI-compatible HTTP server exposing `POST /inference` (multipart/form-data with an audio file in the `file` field). Returns transcript JSON. The `meeting-api` service ([`services/transcription.py`](../meeting-api/app/services/transcription.py)) calls this every time `egress_ended` fires for a recording.

## How it gets used

```
egress_ended webhook → meeting-api dispatches BackgroundTask
  → ffmpeg: <recording>.mp4 → /tmp/<id>.wav (16 kHz mono)
    → POST WAV to http://whisper:8080/inference
      → save transcript to <recording>.txt
        → email transcript to captured participant addresses (via Resend)
```

The whole pipeline runs in the background; the recording is downloadable as soon as the MP4 is finalized — transcripts arrive a few minutes later.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `MODEL` | `/models/ggml-base.en.bin` | Path to the ggml model file. Swap in `medium.en` for higher accuracy at ~3× CPU cost. |
| `THREADS` | `2` | Sized for the 2-vCPU Hetzner box. Bump on bigger hosts via `WHISPER_THREADS` in `.env`. |
| `PORT` | `8080` | Listen port (internal only). |

## Why baked-in model

The model is downloaded during the Docker build and copied into the final image. Container starts instantly with no internet access required at runtime — important because the reference deployment runs on a small box where the model file download every restart would be wasteful.

## Swapping in a different model

To use `small`, `medium`, or a multilingual model:

1. Edit [`Dockerfile`](Dockerfile): change `bash ./models/download-ggml-model.sh base.en` to e.g. `medium.en` or `medium`.
2. Update the `COPY` line + `MODEL` env to point at the new file.
3. Rebuild: `docker compose build whisper && docker compose up -d whisper`.

For a multilingual model, drop `.en` from the filename. Whisper auto-detects language.

## External whisper instead

Set `WHISPER_URL` in `.env` to point at an external OpenAI-compatible endpoint (a bigger host, a managed transcription service, etc.) and the `meeting-api` service will hit that instead. Set to empty string to disable transcription entirely.
