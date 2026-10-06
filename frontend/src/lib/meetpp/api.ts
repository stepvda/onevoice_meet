import { clearAccessToken, forceBootstrapFromOneWitysk, getAccessToken } from "../auth";
import type {
  AttachmentDto,
  AttendeeDto,
  DecisionDto,
  DocSummary,
  DocumentDto,
  HumanOp,
  JobInfo,
  MeetingType,
  MajorityRule,
  OpsResult,
  OutputDto,
  ReviewDraft,
  ReviewResponse,
  RosterDto,
  SectionDto,
  SegmentDto,
  SeriesDto,
  SessionListItem,
  SessionMeta,
  SessionSettings,
  Snapshot,
  ActionDto,
  VoteMethod,
  BallotChoice,
} from "./types";

/**
 * REST client for the Meet++ v3 API (contract §2). Every call sends the
 * user's JWT (chair endpoints) and, when known, the LiveKit room token in
 * `X-Meet-Room-Token` (room / room-chair endpoints). One silent SSO
 * re-bootstrap is attempted on a 401, like lib/api.ts.
 */

let roomToken: string | null = null;

export function setMeetppRoomToken(token: string | null): void {
  roomToken = token;
}

export function getMeetppRoomToken(): string | null {
  return roomToken;
}

export class MeetppApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

function authHeaders(access: string | null): Record<string, string> {
  const h: Record<string, string> = {};
  if (access) h["Authorization"] = `Bearer ${access}`;
  if (roomToken) h["X-Meet-Room-Token"] = roomToken;
  return h;
}

async function send(path: string, init: RequestInit, json: boolean): Promise<Response> {
  const run = (access: string | null) => {
    const headers: Record<string, string> = { ...authHeaders(access), ...((init.headers as Record<string, string>) ?? {}) };
    if (json && init.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    return fetch(`/api/v1${path}`, { ...init, headers });
  };
  const access = getAccessToken();
  let res = await run(access);
  if (res.status === 401 && access) {
    clearAccessToken();
    const fresh = await forceBootstrapFromOneWitysk().catch(() => null);
    if (fresh) res = await run(fresh);
  }
  return res;
}

async function errorOf(res: Response): Promise<MeetppApiError> {
  let detail = res.statusText || `HTTP ${res.status}`;
  try {
    const data = await res.json();
    if (typeof data?.detail === "string") detail = data.detail;
    else if (data?.detail) detail = JSON.stringify(data.detail);
  } catch {
    /* not JSON */
  }
  return new MeetppApiError(detail, res.status);
}

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await send(path, init, true);
  if (!res.ok) throw await errorOf(res);
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

async function reqBlob(path: string): Promise<Blob> {
  const res = await send(path, {}, false);
  if (!res.ok) throw await errorOf(res);
  return res.blob();
}

async function multipart<T>(path: string, form: FormData): Promise<T> {
  const res = await send(path, { method: "POST", body: form }, false);
  if (!res.ok) throw await errorOf(res);
  return (await res.json()) as T;
}

const body = (b: unknown): RequestInit => ({ body: JSON.stringify(b) });

export interface OutlinePointInput {
  id?: string;
  title: string;
  body?: string | null;
  presenter?: string | null;
  timebox_minutes?: number | null;
  subpoints: Array<{ id?: string; title: string; body?: string | null }>;
}

export interface VoteInput {
  method: VoteMethod;
  for?: number | null;
  against?: number | null;
  abstain?: number | null;
  ballots?: Array<{ name: string; person_key?: string | null; choice: BallotChoice; cast_by?: string | null; proxy?: boolean }>;
  confirmed?: boolean;
}

export const meetppApi = {
  // ── discovery (no auth) ──
  roomActive: (room: string) =>
    req<{ active: boolean; sid?: string; provider_label?: string; consent_version?: string }>(
      `/meetpp/rooms/${encodeURIComponent(room)}/active`,
    ),

  // ── setup and lifecycle (chair) ──
  createSession: (
    meetingId: string,
    b: { template: "agenda" | "goal"; mode?: "lead" | "assist"; series_id?: string; meeting_type?: MeetingType; goal?: string },
  ) =>
    req<{ session: SessionMeta; series: SeriesDto; imported_actions: ActionDto[] }>(`/meetings/${meetingId}/meetpp/sessions`, {
      method: "POST",
      ...body(b),
    }),
  listSessions: (meetingId: string) => req<{ sessions: SessionListItem[] }>(`/meetings/${meetingId}/meetpp/sessions`),
  patchSession: (
    sid: string,
    b: Partial<{ mode: "lead" | "assist"; goal: string; editors: string[]; settings: SessionSettings }>,
  ) => req<SessionMeta>(`/meetpp/sessions/${sid}`, { method: "PATCH", ...body(b) }),
  putOutline: (sid: string, agenda: OutlinePointInput[]) =>
    req<{ sections: SectionDto[] }>(`/meetpp/sessions/${sid}/outline`, { method: "PUT", ...body({ agenda }) }),
  uploadDocument: (sid: string, kind: "agenda" | "previous_notes", file: File) => {
    const form = new FormData();
    form.append("kind", kind);
    form.append("file", file);
    return multipart<{ document: DocumentDto }>(`/meetpp/sessions/${sid}/documents`, form);
  },
  getDocument: (sid: string, docId: string) =>
    req<{ document: DocumentDto; summary?: DocSummary | null; structured?: unknown }>(`/meetpp/sessions/${sid}/documents/${docId}`),
  start: (sid: string) => req<SessionMeta>(`/meetpp/sessions/${sid}/start`, { method: "POST" }),
  pause: (sid: string) => req<SessionMeta>(`/meetpp/sessions/${sid}/pause`, { method: "POST" }),
  resume: (sid: string) => req<SessionMeta>(`/meetpp/sessions/${sid}/resume`, { method: "POST" }),
  end: (sid: string) => req<{ status: string }>(`/meetpp/sessions/${sid}/end`, { method: "POST" }),
  finalise: (sid: string) => req<{ jobs: Record<string, JobInfo> }>(`/meetpp/sessions/${sid}/finalise`, { method: "POST" }),
  deleteSession: (sid: string) => req<void>(`/meetpp/sessions/${sid}`, { method: "DELETE" }),

  // ── live (room / room-chair) ──
  getState: (sid: string) => req<Snapshot>(`/meetpp/sessions/${sid}/state`),
  getTranscript: (sid: string, after: number | null, limit = 1000) =>
    req<{ segments: SegmentDto[]; next_after: number | null }>(
      `/meetpp/sessions/${sid}/transcript?${after !== null ? `after=${after}&` : ""}limit=${limit}`,
    ),
  consent: (sid: string, b: { decision: "accept" | "opt_out"; person_key: string; name?: string }) =>
    req<{ ok: boolean }>(`/meetpp/sessions/${sid}/consent`, { method: "POST", ...body(b) }),
  position: (sid: string, b: { action: "next" | "back" | "move"; section_id?: string }) =>
    req<{ live_section_id: string | null; version: number }>(`/meetpp/sessions/${sid}/position`, { method: "POST", ...body(b) }),
  positionUndo: (sid: string) =>
    req<{ live_section_id: string | null; version: number }>(`/meetpp/sessions/${sid}/position/undo`, { method: "POST" }),
  proposal: (sid: string, b: { pid: string; accept: boolean }) =>
    req<{ ok: boolean }>(`/meetpp/sessions/${sid}/proposal`, { method: "POST", ...body(b) }),
  ops: (sid: string, ops: HumanOp[]) => req<OpsResult>(`/meetpp/sessions/${sid}/ops`, { method: "POST", ...body({ ops }) }),
  compose: (sid: string, sectionId: string) =>
    req<{ status: string }>(`/meetpp/sessions/${sid}/sections/${sectionId}/compose`, { method: "POST" }),
  uploadAttachment: (sid: string, blob: Blob, caption: string, sectionId?: string | null) => {
    const form = new FormData();
    form.append("file", blob, "whiteboard.png");
    if (caption) form.append("caption", caption);
    if (sectionId) form.append("section_id", sectionId);
    return multipart<{ attachment: AttachmentDto }>(`/meetpp/sessions/${sid}/attachments`, form);
  },
  attachmentBlob: (url: string) => reqBlob(url.startsWith("/api/v1") ? url.slice("/api/v1".length) : url),
  patchAttachment: (sid: string, id: string, caption: string) =>
    req<{ attachment: AttachmentDto } | AttachmentDto>(`/meetpp/sessions/${sid}/attachments/${id}`, {
      method: "PATCH",
      ...body({ caption }),
    }),
  deleteAttachment: (sid: string, id: string) => req<unknown>(`/meetpp/sessions/${sid}/attachments/${id}`, { method: "DELETE" }),
  patchAttendee: (sid: string, id: string, b: Partial<Omit<AttendeeDto, "id" | "person_key">> & { display_name?: string }) =>
    req<AttendeeDto>(`/meetpp/sessions/${sid}/attendees/${id}`, { method: "PATCH", ...body(b) }),

  // ── review and outputs (chair) ──
  getReview: (sid: string) => req<ReviewResponse>(`/meetpp/sessions/${sid}/review`),
  putReview: (sid: string, draft: ReviewDraft) => req<unknown>(`/meetpp/sessions/${sid}/review`, { method: "PUT", ...body(draft) }),
  putVote: (decisionId: string, vote: VoteInput) =>
    req<DecisionDto>(`/meetpp/decisions/${decisionId}/vote`, { method: "PUT", ...body(vote) }),
  putSeriesRules: (seriesId: string, b: { meeting_type?: MeetingType; majority_rule?: MajorityRule; quorum_required?: number | null }) =>
    req<SeriesDto>(`/meetpp/series/${seriesId}/rules`, { method: "PUT", ...body(b) }),
  getRoster: (seriesId: string) => req<{ roster: RosterDto[] }>(`/meetpp/series/${seriesId}/roster`),
  patchRoster: (seriesId: string, rosterId: string, b: { voting?: boolean; active?: boolean; display_name?: string }) =>
    req<RosterDto>(`/meetpp/series/${seriesId}/roster/${rosterId}`, { method: "PATCH", ...body(b) }),
  reportPdf: (sid: string) => reqBlob(`/meetpp/sessions/${sid}/report.pdf`),
  publish: (sid: string) =>
    req<{ published_at: string; outputs: OutputDto[]; email_results: Array<Record<string, unknown>> }>(
      `/meetpp/sessions/${sid}/publish`,
      { method: "POST" },
    ),
  getOutputs: (sid: string) => req<{ outputs: OutputDto[] } & Record<string, unknown>>(`/meetpp/sessions/${sid}/outputs`),
  outputBlob: (sid: string, oid: string) => reqBlob(`/meetpp/sessions/${sid}/outputs/${oid}`),
  exportJson: (sid: string) => req<Record<string, unknown>>(`/meetpp/sessions/${sid}/export.json`),

  // ── admin (used by AdminPanel) ──
  adminStatus: () =>
    req<{
      enabled: boolean;
      active_sessions: Array<{ sid: string; meeting_id: string; status: string; phase: string; language: string }>;
      agent_health: Record<string, unknown> | null;
      breaker: string;
      tokens_today: number;
      rejected_ops: Array<{ op_type: string; reason: string | null; created_at: string | null }>;
    }>("/admin/meetpp/status"),
};
