import type { RoomConnectOptions, RoomOptions } from "livekit-client";
import type { AnonTokenResponse } from "./api";
import { usePreferences } from "./preferences";

/**
 * Kick off `getUserMedia` early to overlap camera/mic hardware init with
 * the token fetch + route transition + LiveKit WS handshake. Without
 * this, LiveKit calls `getUserMedia` only *after* the WS connect
 * succeeds, and a cold camera takes 1-3 seconds to spin up — total
 * "click Join → camera publishing" is 4-7s.
 *
 * Approach: acquire the stream, hold it for 500 ms (long enough to fully
 * initialise the OS camera driver), then release. On macOS / Windows /
 * Linux the device stays warm for several seconds after release, so when
 * LiveKit's own `getUserMedia` call lands a moment later it returns in
 * ~100 ms instead of seconds.
 *
 * Idempotent: only prewarms once every 30 s, since holding two streams
 * to the same camera in close succession can wedge some macOS drivers.
 * Silent on permission denial — the Lobby's existing "Test mic & camera"
 * flow surfaces those errors properly.
 */
let lastPrewarm = 0;
export function prewarmMedia() {
  if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) return;
  const now = Date.now();
  if (now - lastPrewarm < 30_000) return;
  lastPrewarm = now;
  navigator.mediaDevices
    .getUserMedia({ audio: true, video: true })
    .then((stream) => {
      // Release after a short hold so the driver finishes initialising
      // before we drop our claim — but well before LiveKit's own
      // getUserMedia call lands on the device.
      setTimeout(() => {
        for (const t of stream.getTracks()) t.stop();
      }, 500);
    })
    .catch(() => {
      // Permission denied / no device — fail silently. Either the Lobby
      // already surfaced the issue, or LiveKit will when it tries.
    });
}

/**
 * Build LiveKit RoomOptions + ConnectOptions, folding in the user's
 * persisted preferences so AV/network toggles take effect on join.
 *
 * Mid-meeting changes to these prefs require leaving and rejoining the
 * meeting — they only apply at connection / publish time.
 */
export function roomOptions(tokenResp: AnonTokenResponse): {
  serverUrl: string;
  token: string;
  roomOptions: RoomOptions;
  connectOptions: RoomConnectOptions;
} {
  const prefs = usePreferences.getState();
  const connectOptions: RoomConnectOptions = {
    autoSubscribe: true,
  };
  const rtcConfig: RTCConfiguration = {};
  if (tokenResp.ice_servers) {
    rtcConfig.iceServers = [
      {
        urls: tokenResp.ice_servers.urls,
        username: tokenResp.ice_servers.username,
        credential: tokenResp.ice_servers.credential,
      },
    ];
  }
  if (prefs.network.forceRelay) {
    rtcConfig.iceTransportPolicy = "relay";
  }
  if (rtcConfig.iceServers || rtcConfig.iceTransportPolicy) {
    connectOptions.rtcConfig = rtcConfig;
  }

  return {
    serverUrl: tokenResp.livekit_url,
    token: tokenResp.token,
    roomOptions: {
      adaptiveStream: true,
      dynacast: true,
      audioCaptureDefaults: {
        autoGainControl: prefs.av.autoGainControl,
        echoCancellation: prefs.av.echoCancellation,
        noiseSuppression: prefs.av.noiseSuppression,
      },
      publishDefaults: {
        simulcast: prefs.network.simulcastEnabled,
      },
    },
    connectOptions,
  };
}
