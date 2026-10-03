import { getAccessToken } from "../auth";
import type { BoardState, SessionMeta, TranscriptSegment } from "./types";

let roomToken: string | null = null;

/** The LiveKit room token is captured from sessionStorage by the provider
 * before it is cleared, so room-scoped Meet++ endpoints can authenticate. */
export function setMeetppRoomToken(token: string | null): void {
  roomToken = token;
}

function headers(json = true): Record<string, string> {
  const h: Record<string, string> = {};
  const access = getAccessToken();
  if (access) h["Authorization"] = `Bearer ${access}`;
  if (roomToken) h["X-Meet-Room-Token"] = roomToken;
  if (json) h["Content-Type"] = "application/json";
  return h;
}

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api/v1${path}`, { ...init, headers: { ...headers(), ...(init.headers as Record<string, string>) } });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail ?? detail;
    } catch {
      /* not json */
    }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export interface CreateSessionResult {
  session: SessionMeta;
  imported_actions: unknown[];
  provider_label: string;
}

export const meetppApi = {
  roomActive: (room: string) =>
    req<{ active: boolean; sid?: string; provider_label?: string }>(`/meetpp/rooms/${encodeURIComponent(room)}/active`),

  createSession: (meetingId: string, body: { template: string; mode: string; language: string; series_id?: string | null; goal?: string | null }) =>
    req<CreateSessionResult>(`/meetings/${meetingId}/meetpp/sessions`, { method: "POST", body: JSON.stringify(body) }),

  listSessions: (meetingId: string) => req<{ sessions: SessionMeta[] }>(`/meetings/${meetingId}/meetpp/sessions`),

  patchSession: (sid: string, body: Record<string, unknown>) =>
    req<SessionMeta>(`/meetpp/sessions/${sid}`, { method: "PATCH", body: JSON.stringify(body) }),

  start: (sid: string) => req<{ ok: boolean; session: SessionMeta }>(`/meetpp/sessions/${sid}/start`, { method: "POST" }),
  pause: (sid: string) => req<{ ok: boolean }>(`/meetpp/sessions/${sid}/pause`, { method: "POST" }),
  resume: (sid: string) => req<{ ok: boolean }>(`/meetpp/sessions/${sid}/resume`, { method: "POST" }),
  end: (sid: string) => req<{ ok: boolean; final: unknown }>(`/meetpp/sessions/${sid}/end`, { method: "POST" }),
  finalise: (sid: string) => req<{ ok: boolean; final: unknown }>(`/meetpp/sessions/${sid}/finalise`, { method: "POST" }),

  uploadDocument: async (sid: string, kind: "agenda" | "previous_notes", file: File) => {
    const form = new FormData();
    form.append("kind", kind);
    form.append("file", file);
    const res = await fetch(`/api/v1/meetpp/sessions/${sid}/documents`, { method: "POST", headers: headers(false), body: form });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? "upload failed");
    return (await res.json()) as { document: { id: string; status: string } };
  },

  getDocument: (sid: string, docId: string) => req<Record<string, unknown>>(`/meetpp/sessions/${sid}/documents/${docId}`),

  putAgenda: (sid: string, items: Array<Record<string, unknown>>) =>
    req<{ ok: boolean; announcement: unknown }>(`/meetpp/sessions/${sid}/agenda`, { method: "PUT", body: JSON.stringify({ items }) }),

  getState: (sid: string, since?: number) =>
    req<BoardState & { v?: number; type?: string }>(`/meetpp/sessions/${sid}/state${since !== undefined ? `?since=${since}` : ""}`),

  getTranscript: (sid: string, after = 0) =>
    req<{ segments: TranscriptSegment[] }>(`/meetpp/sessions/${sid}/transcript?after=${after}`),

  consent: (sid: string, decision: "accept" | "opt_out") =>
    req<{ ok: boolean }>(`/meetpp/sessions/${sid}/consent`, { method: "POST", body: JSON.stringify({ decision }) }),

  ops: (sid: string, ops: Array<Record<string, unknown>>) =>
    req<{ applied: unknown[]; rejected: unknown[]; version: number }>(`/meetpp/sessions/${sid}/ops`, { method: "POST", body: JSON.stringify({ ops }) }),

  phase: (sid: string, body: { to: string; item_id?: string | null; accept: boolean }) =>
    req<{ ok: boolean }>(`/meetpp/sessions/${sid}/phase`, { method: "POST", body: JSON.stringify(body) }),

  boardToMain: (sid: string) =>
    req<{ ok: boolean }>(`/meetpp/sessions/${sid}/board-to-main`, { method: "POST" }),

  uploadAttachment: async (sid: string, blob: Blob, caption: string) => {
    const form = new FormData();
    form.append("file", blob, "whiteboard.png");
    form.append("caption", caption);
    const res = await fetch(`/api/v1/meetpp/sessions/${sid}/attachments`, { method: "POST", headers: headers(false), body: form });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? "snapshot failed");
    return (await res.json()) as { attachment: unknown };
  },

  getOutputs: (sid: string) =>
    req<{
      status: string;
      finalised: boolean;
      ready: boolean;
      outputs: Array<{ id: string; kind: string; filename: string; download_url: string }>;
      auto_sent: boolean;
      recurring: boolean;
    }>(`/meetpp/sessions/${sid}/outputs`),

  patchAttachment: (sid: string, aid: string, caption: string) =>
    req<{ attachment: unknown }>(`/meetpp/sessions/${sid}/attachments/${aid}`, { method: "PATCH", body: JSON.stringify({ caption }) }),
  deleteAttachment: (sid: string, aid: string) =>
    req<{ ok: boolean }>(`/meetpp/sessions/${sid}/attachments/${aid}`, { method: "DELETE" }),
  patchAttendee: (sid: string, aid: string, body: Record<string, unknown>) =>
    req<{ attendee: unknown }>(`/meetpp/sessions/${sid}/attendees/${aid}`, { method: "PATCH", body: JSON.stringify(body) }),
  regenerateMinutes: (sid: string, itemId: string) =>
    req<{ minute: unknown }>(`/meetpp/sessions/${sid}/minutes/${itemId}/regenerate`, { method: "POST" }),

  adminStatus: () =>
    req<{
      enabled: boolean;
      active_sessions: Array<{ sid: string; meeting_id: string; status: string; phase: string; language: string }>;
      agent_health: Record<string, unknown> | null;
      breaker: string;
      tokens_today: number;
      rejected_ops: Array<{ op_type: string; reason: string | null; created_at: string | null }>;
    }>("/admin/meetpp/status"),

  getReview: (sid: string) => req<Record<string, unknown>>(`/meetpp/sessions/${sid}/review`),
  putReview: (sid: string, body: Record<string, unknown>) =>
    req<{ ok: boolean }>(`/meetpp/sessions/${sid}/review`, { method: "PUT", body: JSON.stringify(body) }),
  publish: (sid: string) => req<Record<string, unknown>>(`/meetpp/sessions/${sid}/publish`, { method: "POST" }),

  exportJson: (sid: string) => req<Record<string, unknown>>(`/meetpp/sessions/${sid}/export.json`),
  exportMd: (sid: string) => req<{ markdown: string }>(`/meetpp/sessions/${sid}/export.md`),
};
