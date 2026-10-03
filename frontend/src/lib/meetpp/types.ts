export type MeetppTab =
  | "agenda"
  | "decisions"
  | "actions"
  | "attendance"
  | "minutes"
  | "attachments"
  | "transcript";

export interface SessionMeta {
  id: string;
  editors?: string[];
  status: "setup" | "running" | "paused" | "finalising" | "review" | "published" | "aborted";
  template: "agenda" | "goal";
  mode: "lead" | "assist";
  language: string;
  goal: string | null;
  phase: string;
  phase_index: number;
  current_item_id: string | null;
  version: number;
  settings: Record<string, boolean>;
  started_at: string | null;
  ended_at: string | null;
}

export interface AgendaItem {
  id: string;
  position: number;
  title: string;
  presenter: string | null;
  timebox: number | null;
  outcome: string | null;
  status: "pending" | "active" | "done" | "deferred";
  source: string;
  started_at: string | null;
  locked: boolean;
}

export interface Decision {
  id: string;
  ref: string;
  text: string;
  rationale: string | null;
  item_id: string | null;
  status: "proposed" | "confirmed" | "rejected";
  origin: string;
  locked: boolean;
  evidence: number[];
  previous?: boolean;
}

export interface ActionItem {
  id: string;
  ref: string;
  /** Session that created the action; used to split previous vs new. */
  session_id?: string | null;
  title: string;
  owner: string | null;
  due: string | null;
  item_id: string | null;
  status: string;
  review_status: string | null;
  origin: string;
  locked: boolean;
  note: string | null;
  evidence: number[];
}

export interface Attendee {
  id: string;
  person_key: string;
  identities?: string[];
  name: string;
  email: string | null;
  role: string | null;
  presence: string;
  talk_seconds: number;
  opted_out: boolean;
  required_now: boolean;
  required_next: boolean;
  required_reason: string | null;
}

export interface Minute {
  id: string;
  item_id: string | null;
  body_md: string;
  version: number;
  status: string;
  locked: boolean;
}

export interface Attachment {
  id: string;
  item_id: string | null;
  kind: string;
  filename: string;
  caption: string | null;
  author: string | null;
  url: string;
}

export interface TranscriptSegment {
  seq: number;
  identity: string;
  name: string | null;
  text: string;
  t_end: string | null;
  t_start?: string | null;
  duration_ms?: number | null;
}

export interface Caption {
  seq: number;
  identity: string;
  name: string;
  text: string;
  t: string;
  t_start?: string | null;
  duration_ms?: number | null;
}

export interface BoardState {
  session: SessionMeta;
  agenda: AgendaItem[];
  decisions: Decision[];
  previous_decisions: Decision[];
  actions: ActionItem[];
  attendance: Attendee[];
  minutes: Minute[];
  attachments: Attachment[];
}

export interface FocusHint {
  tab: MeetppTab;
  id: string | null;
  prio: number;
}

export interface Proposal {
  pid: string;
  to: string;
  item_id: string | null;
  reason: string;
  confidence: number;
  auto_at: string | null;
}

export interface Announcement {
  aid: string;
  title: string;
  subtitle: string;
  audio_url: string | null;
  duration_ms: number;
}

export interface AgentStatus {
  status: "listening" | "thinking" | "behind" | "paused" | "offline" | "budget";
  backlog_s: number | null;
}
