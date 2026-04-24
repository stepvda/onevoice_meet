import { getAccessToken } from "./auth";

export interface AnonTokenResponse {
  livekit_url: string;
  token: string;
  room_name: string;
  ice_servers: {
    username: string;
    credential: string;
    urls: string[];
    ttl: number;
  } | null;
}

export interface MeetingOut {
  id: string;
  room_name: string;
  display_title: string;
  owner_user_id: string;
  is_active: boolean;
  require_password: boolean;
  max_participants: number;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const tok = getAccessToken();
  if (tok) headers.set("Authorization", `Bearer ${tok}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");

  const res = await fetch(path, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  createMeeting: (body: { display_title: string; password?: string }) =>
    request<{ meeting: MeetingOut; join_url: string }>("/api/v1/meetings", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  listMeetings: () => request<MeetingOut[]>("/api/v1/meetings"),

  ownerToken: (meetingId: string) =>
    request<AnonTokenResponse>(`/api/v1/meetings/${meetingId}/token`, { method: "POST" }),

  anonToken: (roomName: string, body: { display_name: string; email?: string; password?: string }) =>
    request<AnonTokenResponse>(`/api/v1/rooms/${roomName}/anon-token`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  mute: (meetingId: string, body: { participant_identity: string; mute: boolean }) =>
    request(`/api/v1/meetings/${meetingId}/mute`, { method: "POST", body: JSON.stringify(body) }),

  muteAll: (meetingId: string) =>
    request(`/api/v1/meetings/${meetingId}/mute-all`, { method: "POST" }),

  kick: (meetingId: string, participant_identity: string) =>
    request(`/api/v1/meetings/${meetingId}/kick`, {
      method: "POST",
      body: JSON.stringify({ participant_identity }),
    }),

  endMeeting: (meetingId: string) =>
    request(`/api/v1/meetings/${meetingId}`, { method: "DELETE" }),

  deleteMeeting: (meetingId: string) =>
    request(`/api/v1/meetings/${meetingId}`, { method: "DELETE" }),

  setPresenter: (meetingId: string, participant_identity: string | null) =>
    request(`/api/v1/meetings/${meetingId}/presenter`, {
      method: "POST",
      body: JSON.stringify({ participant_identity }),
    }),

  startRecording: (meetingId: string) =>
    request(`/api/v1/meetings/${meetingId}/recordings:start`, { method: "POST" }),

  stopRecording: (meetingId: string) =>
    request(`/api/v1/meetings/${meetingId}/recordings:stop`, { method: "POST" }),

  listRecordings: () => request<unknown[]>("/api/v1/recordings"),
};
