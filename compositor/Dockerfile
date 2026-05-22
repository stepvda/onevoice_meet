FROM node:20-bookworm-slim

# Google Chrome Stable (NOT Debian's `chromium`). Reason: Debian builds
# Chromium without the proprietary H.264 / AAC codecs, and the LiveKit
# Ingress (URL_INPUT) publishes the playback video track as H.264. A
# Chromium without H.264 ends up with `readyState=0` on the source
# video element and `videoWidth=0`, which makes `drawImage(video,…)`
# render a black frame. Google Chrome ships those codecs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        fonts-liberation \
        fonts-noto-color-emoji \
        xvfb \
        xauth \
        dbus \
        dbus-x11 \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev

COPY server.js entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh

ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/google-chrome-stable
ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV DISPLAY=:99

EXPOSE 8090

# Custom entrypoint launches Xvfb in the background, waits for the
# socket, then execs node. We tried `xvfb-run` but it hangs as PID 1
# in this container, leaving node never spawned. The script also
# forwards SIGTERM so compose stop / restart shuts everything down
# cleanly.
CMD ["/app/entrypoint.sh"]
