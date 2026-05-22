#!/bin/sh
# Start Xvfb in the background on display :99, then exec node. This is
# the equivalent of `xvfb-run` but plays nicely with Docker as PID 1
# (xvfb-run wrapper hangs in some Debian / containerised setups,
# leaving Node never spawned).

set -e

export DISPLAY=:99

# Launch Xvfb. `-nolisten tcp` is a security default; we don't need
# any network listeners on the X server. The framebuffer matches the
# Chrome window size set by the Node app.
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
XVFB_PID=$!

# Brief poll until the X server is actually accepting connections.
# 50 iterations * 100ms = 5s ceiling, plenty for Xvfb cold start.
i=0
while [ $i -lt 50 ]; do
    if [ -e /tmp/.X11-unix/X99 ]; then
        break
    fi
    i=$((i + 1))
    sleep 0.1
done

# Forward SIGTERM / SIGINT to both Xvfb and the Node child so compose
# stop / restart shuts down cleanly.
cleanup() {
    if kill -0 "$XVFB_PID" 2>/dev/null; then
        kill "$XVFB_PID" 2>/dev/null || true
    fi
}
trap cleanup TERM INT EXIT

exec node server.js
