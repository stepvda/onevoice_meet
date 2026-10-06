# meetpp-speech — Meet++ tier-2 speech service

FastAPI service for the association's **Mac Studio** (Apple Silicon). It runs
**mlx-whisper `large-v3-turbo`** (STT) and **Kokoro-82M, voice `am_michael`**
(TTS). meetpp-agent on turn.witysk.org calls it over the existing WireGuard
tunnel (`10.88.0.1` → `10.88.0.2:9310`) with HMAC-signed requests.
Specification: `docs/meetpp-v3-contract.md` §6.2 and §8, FDD v3.1 §7.2, §7.4 and §11.2.

- No ffmpeg, Homebrew or sudo needed. Audio is decoded and encoded with `soundfile`, whose
  wheel bundles libsndfile with Ogg/Opus.
- Both models are loaded once at start-up and warmed up before the port answers.
- Memory: about 2.8 GB physical footprint (whisper weights 1.6 GB + MLX cache ≤ ~0.5 GB + Kokoro).
- Disk: `.venv` ~650 MB, `models/` 347 MB, whisper weights 1.5 GB (`~/.cache/huggingface`).
- Never use openai-whisper with `--device mps` on Apple Silicon (NaN logits). This service uses MLX.

## API

All POSTs need `X-Meetpp-Timestamp: <unix seconds>` and `X-Meetpp-Signature` (HMAC version 2):

```
X-Meetpp-Signature = hex(HMAC_SHA256(MEETPP_SPEECH_SECRET,
    "v2\n" + ts + "\n" + METHOD + "\n" + target + "\n" + hex(SHA256(raw_body))))
```

`target` is the raw request path plus `?` + the raw query string when there is one, exactly as sent
(e.g. `/transcribe?language=en&prompt=Meeting%20of%20…`), so `language` and `prompt` are signed too.
A timestamp more than ±60 s from the Mac's clock, a bad signature, or a signature that was already
accepted (replay; a retry must be signed again) gets **401**.

| Endpoint | Request | Response |
|---|---|---|
| `POST /transcribe?language=en&prompt=…` | body = `audio/ogg` (Opus) or `audio/wav` (also FLAC/AIFF) bytes | `{text, avg_logprob, duration_s, rtf, model, repetition}` + extras below |
| `POST /tts` | JSON `{text, voice:"am_michael", format:"ogg"}` (`format` `ogg`/`wav`, optional `speed` 0.5–2) | `audio/ogg` (Opus) bytes; `audio/wav` if Opus encoding is unavailable or `format:"wav"` |
| `GET /health` | no auth | `{ok, model:"large-v3-turbo", tts:"kokoro"\|"off", queue, busy, rtf_p50, …}` |

`/transcribe` details:
- Audio is decoded to 16 kHz mono float32 and passed to mlx-whisper as an array. Decoding uses
  `language` (default `en`) and `initial_prompt = prompt`; only the last 600 characters of the
  prompt are kept. Temperature fallback is 0.0 → 0.2 → 0.4 (on compression ratio > 2.4 or
  avg log-prob < −1.0), and `condition_on_previous_text=False`.
- `repetition: true` means the text contained a loop: the same 3–6-word n-gram ≥ 4 times in a row
  (also 2 words ≥ 6×, 1 word ≥ 8×). `text` is then cut just before the loop, and the original is in
  `text_raw`. FDD §7.4 says to use the tier-1 text for that utterance.
- Extra response fields: `no_speech_prob`, `compression_ratio`, `temperature`, `wait_s`
  (queue wait), and `text_raw`/`repetition_ngram`/`repetition_count` when a loop is found.
- `avg_logprob` is `null` when Whisper found no speech; `text` is then `""`.
- Errors: 401 (auth), 413 (body > 4 MB or audio > 300 s, checked from the header before decoding),
  415 (undecodable audio, more than 2 channels or a sample rate above 96 kHz),
  422 (unknown language), 503 + `Retry-After: 2` (queue full), 500 (model failure).

Concurrency: at most **2 transcriptions run at once**, each in a worker thread.
Requests that must wait may hold **up to 30 s of audio** in total; beyond that the
request gets **503**. TTS runs one request at a time, with at most 4 waiting.
`/health` → `busy` = transcriptions running, `queue` / `queue_s` = waiting requests / seconds of audio.

Privacy: the access log records method, path, status and duration only. It never logs the query
string (the prompt is meeting text) or transcripts. The weights are pre-downloaded, and
`run.sh` sets `HF_HUB_OFFLINE=1`, so the running service never contacts Hugging Face.

## Install on the Mac Studio

Prerequisites: an Apple Silicon Mac with `curl` and `openssl` (both ship with macOS). If no
Python 3.11 or 3.12 is found, `install.sh` installs **uv** with Astral's official standalone
installer into `~/.local/bin`, without editing your shell profile. uv then provides Python 3.12
in `~/.local/share/uv/python`. This is the normal path on the Mac Studio, whose only Python is
`/usr/bin/python3` 3.9.

```bash
# 1. Copy this directory to the Mac. Use a short path outside ~/Documents and ~/Desktop
#    (macOS privacy protection blocks LaunchAgents there).
rsync -a --exclude .venv --exclude models --exclude __pycache__ meetpp-speech/ macstudio:~/meetpp-speech/

# 2. On the Mac, as the user that stays logged in (the mail-app user):
cd ~/meetpp-speech
./install.sh --launchd
```

`install.sh --launchd` does the following:
1. Creates `.venv` with uv, or with a local `python3.12`/`3.11` via `python -m venv`.
2. Installs the pinned `requirements.txt` with `--no-deps`. That file is a full lock, and
   `--no-deps` skips torch, which mlx-whisper declares but never imports.
3. Downloads `kokoro-v1.0.onnx` and `voices-v1.0.bin` from the kokoro-onnx GitHub release into
   `models/` and checks their SHA-256.
4. Pre-downloads `mlx-community/whisper-large-v3-turbo` into the Hugging Face cache.
5. Creates `~/.config/meetpp-speech/env` (mode 600) with a new random secret and
   `MEETPP_SPEECH_BIND=10.88.0.2`, unless the file already exists.
6. Installs `~/Library/LaunchAgents/org.witysk.meetpp-speech.plist` and loads it. The agent has
   RunAtLoad and KeepAlive, runs under `/usr/bin/caffeinate -is` so the Mac does not sleep, and
   logs to `~/Library/Logs/meetpp-speech.log`.

Then:

```bash
# 3. Put the same secret into meetpp-agent's environment on turn.witysk.org:
grep MEETPP_SPEECH_SECRET ~/.config/meetpp-speech/env
#    turn: MEETPP_SPEECH_URL=http://10.88.0.2:9310  MEETPP_SPEECH_SECRET=<same value>

# 4. Check it (ready ~10-20 s after load):
curl -s http://10.88.0.2:9310/health          # on the Mac
ssh turn 'curl -s http://10.88.0.2:9310/health'   # from turn (and from inside the agent container)

# 5. Optional: run the test-suite on the Mac (starts its own instance on a free 127.0.0.1 port)
./install.sh --dev && .venv/bin/python -m pytest -q
```

A LaunchAgent starts when its user logs in to the desktop. Keep that account logged in, or enable
automatic login, as for the mail app. If you install over SSH while nobody is logged in to the
desktop, `install.sh` loads the agent into the `user/<uid>` domain instead; it moves to `gui/<uid>`
at the next desktop login. If `10.88.0.2` is not up yet (WireGuard at boot), the
service retries the bind every 5 s before it loads any model.

## Configuration (`~/.config/meetpp-speech/env`, sourced by `run.sh`)

| Variable | Default | Notes |
|---|---|---|
| `MEETPP_SPEECH_SECRET` | — (required) | shared with meetpp-agent; 32+ random chars |
| `MEETPP_SPEECH_BIND` | `127.0.0.1` | production `10.88.0.2` (WireGuard address only) |
| `MEETPP_SPEECH_PORT` | `9310` | |
| `MEETPP_SPEECH_MODEL` | `mlx-community/whisper-large-v3-turbo` | rerun `install.sh` after changing it (pre-download) |
| `KOKORO_MODEL` / `KOKORO_VOICES` | `models/kokoro-v1.0.onnx` / `models/voices-v1.0.bin` | |
| `MEETPP_SPEECH_MAX_PARALLEL` | `2` | concurrent transcriptions |
| `MEETPP_SPEECH_QUEUE_S` | `30` | seconds of waiting audio before 503 |
| `MEETPP_SPEECH_MAX_AUDIO_S` | `300` | longest single request |
| `MEETPP_SPEECH_MLX_CACHE_MB` | `512` | MLX buffer-cache cap (memory vs. speed) |
| `KOKORO_THREADS` | `4` | onnxruntime threads for TTS |
| `MEETPP_SPEECH_LOG_LEVEL` | `INFO` | |

## Operations

```bash
U=gui/$(id -u)/org.witysk.meetpp-speech
launchctl print $U | grep -E 'state|pid|last exit'   # status
launchctl kickstart -k $U                             # restart (e.g. after editing the env file)
launchctl bootout $U                                  # stop until next login / bootstrap
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.witysk.meetpp-speech.plist   # start again
tail -f ~/Library/Logs/meetpp-speech.log
```

**Upgrade:** copy the new directory over the old one (keep `.venv` and `models/`), then run
`./install.sh --launchd`. It reuses `.venv`, reinstalls the pins, verifies the models, keeps the env
file and restarts the agent. Use `./install.sh --fresh --launchd` to rebuild the venv from scratch.

**Uninstall:**
```bash
launchctl bootout gui/$(id -u)/org.witysk.meetpp-speech
rm ~/Library/LaunchAgents/org.witysk.meetpp-speech.plist
rm -rf ~/meetpp-speech ~/.config/meetpp-speech ~/Library/Logs/meetpp-speech.log ~/Library/Caches/meetpp-speech
rm -rf ~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo
# only if uv was installed just for this:  rm -rf ~/.local/bin/uv ~/.local/bin/uvx ~/.local/share/uv ~/.cache/uv
```

## Network and firewall

The service binds to `10.88.0.2` only, so it is reachable only through the WireGuard tunnel. Every
request must also carry a valid HMAC. If the Mac's WireGuard peer for turn has
`AllowedIPs = 10.88.0.1/32`, only turn can send traffic into the tunnel. To enforce
"port 9310 only from 10.88.0.1" at the packet level as well, an administrator can add a pf anchor:

```
# /etc/pf.anchors/org.witysk.meetpp-speech   (then: anchor + load anchor lines in /etc/pf.conf, sudo pfctl -f /etc/pf.conf -e)
pass  in quick proto tcp from 10.88.0.1 to 10.88.0.2 port 9310
block in quick proto tcp to 10.88.0.2 port 9310
```

If the macOS Application Firewall is on, allow incoming connections for the venv's Python
(`.venv/bin/python` resolves to `~/.local/share/uv/python/cpython-3.12*/bin/python3.12`). Then
check from turn with `curl http://10.88.0.2:9310/health`.

## Calling it by hand (curl + openssl)

```bash
SECRET=$(sed -n 's/^MEETPP_SPEECH_SECRET=//p' ~/.config/meetpp-speech/env)
URL=http://10.88.0.2:9310

# transcribe (signed: method, path + query exactly as sent, SHA-256 of the body)
TARGET='/transcribe?language=en&prompt=Meeting%20of%20the%20Witysk%20association.'
TS=$(date +%s)
BODY_SHA=$(shasum -a 256 utterance.ogg | cut -d' ' -f1)
SIG=$(printf 'v2\n%s\nPOST\n%s\n%s' "$TS" "$TARGET" "$BODY_SHA" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.*= //')
curl -sS -X POST "$URL$TARGET" \
  -H "Content-Type: audio/ogg" -H "X-Meetpp-Timestamp: $TS" -H "X-Meetpp-Signature: $SIG" \
  --data-binary @utterance.ogg

# tts
BODY='{"text":"Item three: approval of the budget.","voice":"am_michael","format":"ogg"}'
TS=$(date +%s)
BODY_SHA=$(printf '%s' "$BODY" | shasum -a 256 | cut -d' ' -f1)
SIG=$(printf 'v2\n%s\nPOST\n/tts\n%s' "$TS" "$BODY_SHA" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.*= //')
curl -sS -X POST "$URL/tts" -H "Content-Type: application/json" \
  -H "X-Meetpp-Timestamp: $TS" -H "X-Meetpp-Signature: $SIG" --data "$BODY" -o clip.ogg
```

(`openssl -hmac` puts the secret on the command line. That is fine for debugging but not for
scripts.) `client_example.py` is a stdlib-only reference client that signs requests the way
meetpp-agent must (`python3 client_example.py health|transcribe FILE --prompt …|tts TEXT -o out.ogg`).

## Notes for the agent (caller)

- Whisper treats the prompt as text that came *just before* the audio. If the prompt ends
  mid-phrase with the words the utterance starts with, Whisper can skip those words. Example:
  prompt "…of the board" and audio "The board approved…" gives "approved…". Send previous
  *transcript* text that ends at the end of the previous utterance, and don't append glossary
  fragments after it. Put the glossary first and the rolling context last.
- Limit your own concurrency to 2 (the final pass included) and retry 503 after `Retry-After`.
- Short utterances have a higher RTF: the encoder always processes a 30 s window. On an M1 a 3 s
  utterance takes ~1.5 s and an 18 s utterance ~2 s.

## Development

```bash
./install.sh --dev
.venv/bin/python -m pytest -q -m "not integration"   # fast, fake engines (~4 s)
.venv/bin/python -m pytest -q -s                      # + real models: starts ./run.sh on a free port
MEETPP_SPEECH_TEST_URL=http://127.0.0.1:9310 MEETPP_SPEECH_TEST_SECRET=… .venv/bin/python -m pytest -m integration -s
```
