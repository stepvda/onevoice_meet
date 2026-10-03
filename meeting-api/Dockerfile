FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# `ffmpeg` is used by app.services.transcription to decode the recorded MP4
# into a 16 kHz mono WAV before POSTing it to the local whisper.cpp server.
# It is also used by app.services.previews to render On Demand social-card
# media (poster/GIF/clip) on the 2-vCPU production host.
# Meet++ adds WeasyPrint's pango/cairo stack and Noto fonts for the minutes
# and agenda PDFs (about 80 MB).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 libgdk-pixbuf-2.0-0 \
        shared-mime-info fonts-noto-core && \
    rm -rf /var/lib/apt/lists/*

# Hard resource sandbox for the social-preview ffmpeg jobs. A palettegen GIF
# from a 2048x1080 HEVC source once ballooned past 3 GB RSS and put the whole
# host under memory pressure, so every preview encode runs through this
# wrapper: data segment capped at 1.5 GiB, 120 CPU-seconds max, lowest
# priority so live egress/LiveKit always win the CPUs.
RUN printf '#!/bin/sh\nulimit -d 1572864 2>/dev/null || true\nulimit -t 120 2>/dev/null || true\nexec nice -n 19 /usr/bin/ffmpeg "$@"\n' \
        > /usr/local/bin/meet-ffmpeg && chmod +x /usr/local/bin/meet-ffmpeg

WORKDIR /app

COPY pyproject.toml requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app

RUN mkdir -p /var/lib/meet/recordings

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips=*"]
