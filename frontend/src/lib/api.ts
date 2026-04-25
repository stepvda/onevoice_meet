import { bootstrapFromOneWitysk, clearAccessToken, getAccessToken } from "./auth";

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
  branding_url?: string | null;
  list_for_authenticated?: boolean;
  list_for_anonymous?: boolean;
}

export interface PublicMeeting {
  room_name: string;
  display_title: string;
  max_participants: number;
  require_password: boolean;
  branding_url: string | null;
}

export interface PublicRoomInfo {
  room_name: string;
  display_title: string;
  require_password: boolean;
  branding_url: string | null;
}

async function fetchOnce(path: string, init: RequestInit, token: string | null): Promise<Response> {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  return fetch(path, { ...init, headers });
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res = await fetchOnce(path, init, getAccessToken());

  // If the JWT expired, our cached token is stale. one.witysk.org's SPA
  // auto-refreshes its own localStorage token (TokenManager), so a fresh
  // re-bootstrap usually picks up a valid one. Try exactly once, then bail.
  if (res.status === 401) {
    let detail = "";
    try {
      detail = (await res.clone().json())?.detail ?? "";
    } catch {
      /* ignore */
    }
    if (/expired|invalid token/i.test(detail)) {
      clearAccessToken();
      const fresh = await bootstrapFromOneWitysk();
      if (fresh) {
        res = await fetchOnce(path, init, fresh);
      }
    }
  }

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
  createMeeting: (body: {
    display_title: string;
    password?: string;
    list_for_authenticated?: boolean;
    list_for_anonymous?: boolean;
  }) =>
    request<{ meeting: MeetingOut; join_url: string }>("/api/v1/meetings", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updateMeeting: (
    meetingId: string,
    body: {
      display_title?: string;
      list_for_authenticated?: boolean;
      list_for_anonymous?: boolean;
      recording_mode?: "manual" | "auto_on_start" | "off";
    }
  ) =>
    request<MeetingOut>(`/api/v1/meetings/${meetingId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  listMeetings: () => request<MeetingOut[]>("/api/v1/meetings"),

  listDiscoverable: () => request<PublicMeeting[]>("/api/v1/discoverable"),

  listPublicMeetings: () => request<PublicMeeting[]>("/api/v1/public-meetings"),

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

  publishYoutube: (
    recordingId: string,
    body: { title?: string; description?: string; privacy?: "public" | "unlisted" | "private" } = {}
  ) =>
    request<{ ok: boolean; url: string; video_id: string }>(
      `/api/v1/recordings/${recordingId}/publish-youtube`,
      { method: "POST", body: JSON.stringify(body) }
    ),

  /**
   * Download a recording. The endpoint requires a Bearer token, which a
   * plain `<a href>` cannot carry — so we fetch the file with the
   * `Authorization` header, materialise a Blob URL, and trigger a save via
   * a synthesised anchor click. The server's `Content-Disposition` is
   * preserved as the suggested filename if present; otherwise `fallbackName`
   * is used.
   *
   * Caveat: the whole file ends up in browser memory. For typical meeting
   * lengths this is fine (~few hundred MB). For multi-GB files, switch to a
   * service-worker-streamed download or a short-lived signed URL.
   */
  async downloadRecording(recordingId: string, fallbackName: string): Promise<void> {
    const tok = getAccessToken();
    let res = await fetch(`/api/v1/recordings/${recordingId}/download`, {
      headers: tok ? { Authorization: `Bearer ${tok}` } : {},
    });
    if (res.status === 401) {
      // Cached token may be stale; clear it so the bootstrap re-fetches the
      // current value from one.witysk.org's localStorage.
      clearAccessToken();
      const fresh = await bootstrapFromOneWitysk();
      if (fresh) {
        res = await fetch(`/api/v1/recordings/${recordingId}/download`, {
          headers: { Authorization: `Bearer ${fresh}` },
        });
      }
    }
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const data = await res.clone().json();
        detail = data.detail ?? detail;
      } catch {
        /* not JSON */
      }
      throw new Error(detail);
    }

    // Pull a filename out of Content-Disposition if the server set one.
    const cd = res.headers.get("Content-Disposition") || "";
    const m = /filename\*?=(?:UTF-8''|")?([^";]+)/i.exec(cd);
    const filename = m ? decodeURIComponent(m[1].replace(/"$/, "")) : fallbackName;

    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Give the browser a tick to start the download before revoking.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },

  deleteRecording: (recordingId: string) =>
    request(`/api/v1/recordings/${recordingId}`, { method: "DELETE" }),

  invite: (
    meetingId: string,
    body: { emails: string[]; personal_message?: string }
  ) =>
    request<{ ok: boolean; sent: number; failed: string[]; join_url: string }>(
      `/api/v1/meetings/${meetingId}/invite`,
      { method: "POST", body: JSON.stringify(body) }
    ),

  /** Public: meeting metadata for the lobby (no auth). */
  publicRoomInfo: (roomName: string) =>
    request<PublicRoomInfo>(`/api/v1/rooms/${roomName}/info`),

  /** Upload (or replace) a meeting's branding image. */
  async uploadBranding(meetingId: string, file: File): Promise<{ branding_url: string | null }> {
    const tok = getAccessToken();
    const fd = new FormData();
    fd.append("file", file);
    let res = await fetch(`/api/v1/meetings/${meetingId}/branding`, {
      method: "POST",
      headers: tok ? { Authorization: `Bearer ${tok}` } : {},
      body: fd,
    });
    if (res.status === 401) {
      clearAccessToken();
      const fresh = await bootstrapFromOneWitysk();
      if (fresh) {
        res = await fetch(`/api/v1/meetings/${meetingId}/branding`, {
          method: "POST",
          headers: { Authorization: `Bearer ${fresh}` },
          body: fd,
        });
      }
    }
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.clone().json();
        detail = j.detail ?? detail;
      } catch {
        /* ignore */
      }
      throw new Error(detail);
    }
    return res.json();
  },
};
