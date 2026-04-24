import type { RoomConnectOptions, RoomOptions } from "livekit-client";
import type { AnonTokenResponse } from "./api";

export function roomOptions(tokenResp: AnonTokenResponse): {
  serverUrl: string;
  token: string;
  roomOptions: RoomOptions;
  connectOptions: RoomConnectOptions;
} {
  const connectOptions: RoomConnectOptions = {
    autoSubscribe: true,
  };
  if (tokenResp.ice_servers) {
    connectOptions.rtcConfig = {
      iceServers: [
        { urls: tokenResp.ice_servers.urls, username: tokenResp.ice_servers.username, credential: tokenResp.ice_servers.credential },
      ],
    };
  }
  return {
    serverUrl: tokenResp.livekit_url,
    token: tokenResp.token,
    roomOptions: {
      adaptiveStream: true,
      dynacast: true,
    },
    connectOptions,
  };
}
