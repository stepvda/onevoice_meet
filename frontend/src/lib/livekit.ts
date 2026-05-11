import type { RoomConnectOptions, RoomOptions } from "livekit-client";
import type { AnonTokenResponse } from "./api";
import { usePreferences } from "./preferences";

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
