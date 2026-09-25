import type { RoomConnectOptions } from "livekit-client";

export interface CqConnection {
  serverUrl: string;
  token: string;
  connectOptions?: RoomConnectOptions;
}

let connection: CqConnection | null = null;
let reconnecting = false;

export function rememberCqConnection(conn: CqConnection): void {
  connection = conn;
}

export function getCqConnection(): CqConnection | null {
  return connection;
}

export function beginCqReconnect(): void {
  reconnecting = true;
}

export function consumeCqReconnecting(): boolean {
  const value = reconnecting;
  reconnecting = false;
  return value;
}
