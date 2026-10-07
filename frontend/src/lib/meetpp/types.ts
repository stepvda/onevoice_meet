/**
 * Meet++ v3 DTOs and real-time messages (docs/meetpp-v3-contract.md §2–§4).
 * Field names follow the wire format exactly; optional markers are used where
 * the contract allows a field to be absent in a partial delta.
 */

export type Tab = "agenda" | "decisions" | "actions" | "minutes" | "attendance" | "papers";
export const TABS: Tab[] = ["agenda", "decisions", "actions", "minutes", "attendance", "papers"];

export type SessionStatus = "setup" | "running" | "paused" | "finalising" | "review" | "published" | "aborted";
export type SectionStatus = "pending" | "live" | "done" | "deferred" | "skipped";
export type SectionKind =
  | "opening"
  | "previous_actions"
  | "agenda"
  | "aob"
  | "new_actions"
  | "closing"
  | "goal"
  | "deliverables"
  | "next_steps"
  | "planning";
export type MeetingType = "informal" | "board" | "general_assembly";
export type MajorityRule = "ordinary" | "unanimous" | "two_thirds" | "four_fifths";
export type AgentStatusValue = "listening" | "behind" | "reconnecting" | "paused" | "offline";
export type Tier2Status = "up" | "down" | "off";

export interface SessionSettings {
  show_public?: boolean;
  in_recordings?: boolean;
  timebox_nudges?: boolean;
  speak?: boolean;
}

export interface UndoInfo {
  from: string | null;
  to: string | null;
  until: string;
}

export interface ProposalInfo {
  pid: string;
  to: string;
  reason: string;
  confidence: number;
}

export interface AgentSpeaker {
  name: string;
  ok: boolean;
  identity?: string;
}

export interface AgentInfo {
  status: AgentStatusValue;
  backlog_s: number | null;
  speakers: AgentSpeaker[];
  tier2: Tier2Status;
  final_pass?: "running" | "done" | "failed" | null;
}

export interface JobInfo {
  status: "pending" | "running" | "done" | "failed" | "skipped";
  error?: string | null;
  [key: string]: unknown;
}

export interface SessionMeta {
  id: string;
  status: SessionStatus;
  template: "agenda" | "goal";
  mode: "lead" | "assist";
  language: string;
  goal: string | null;
  meeting_type: MeetingType;
  majority_rule: MajorityRule;
  series_id: string;
  series_title: string | null;
  meeting_id: string;
  room: string;
  live_section_id: string | null;
  topic_section_id: string | null;
  started_at: string | null;
  ended_at: string | null;
  published_at: string | null;
  settings: SessionSettings;
  editors: string[];
  undo: UndoInfo | null;
  proposal: ProposalInfo | null;
  agent: AgentInfo | null;
  ai: { status: "ok" | "paused" | "unavailable" } | null;
  jobs: Record<string, JobInfo> | null;
}

export interface SectionDto {
  id: string;
  kind: SectionKind;
  parent_id: string | null;
  position: number;
  number: string | null;
  title: string;
  body: string | null;
  presenter: string | null;
  timebox_minutes: number | null;
  status: SectionStatus;
  started_at: string | null;
  ended_at: string | null;
  elapsed_seconds: number;
  source: string;
  locked: boolean;
  counts: { decisions: number; actions: number };
  /** The section that lists the previous actions: the agenda's own actions
   * point when it has one, else "Previous actions" (v3.2). */
  previous_actions_home?: boolean;
}

export type BallotChoice = "for" | "against" | "abstain" | "not_recorded";

export interface BallotDto {
  name: string;
  person_key: string | null;
  choice: BallotChoice;
  cast_by: string | null;
  proxy: boolean;
}

export type VoteMethod = "voice" | "show_of_hands" | "assent" | "consensus" | "roll_call";

export interface VoteDto {
  method: VoteMethod;
  for: number | null;
  against: number | null;
  abstain: number | null;
  eligible: number | null;
  present: number | null;
  quorum_required: number | null;
  quorum_met: boolean | null;
  result: "adopted" | "rejected" | null;
  outcome_note: string | null;
  confirmed: boolean;
  ballots: BallotDto[];
}

/** `pending` = prefilled "to decide" item from the agenda PDF (contract §1a). */
export type DecisionStatus = "pending" | "proposed" | "adopted" | "rejected" | "withdrawn";

export interface DecisionDto {
  id: string;
  ref: string;
  section_id: string | null;
  title: string;
  resolution: string | null;
  how_taken: string | null;
  status: DecisionStatus;
  decided_at: string | null;
  origin: "ai" | "user" | "pdf" | string;
  confirmed: boolean;
  locked: boolean;
  evidence: number[];
  previous: boolean;
  vote: VoteDto | null;
}

export type ActionStatus = "proposed" | "open" | "in_progress" | "done" | "cancelled";

export interface PersonRef {
  name: string;
  person_key: string | null;
}

export interface ActionDto {
  id: string;
  ref: string;
  section_id: string | null;
  title: string;
  description: string | null;
  assignees: PersonRef[];
  due: string | null;
  status: ActionStatus;
  decision_ref: string | null;
  completed_at: string | null;
  completion_note: string | null;
  progress_notes: string | null;
  origin: string;
  locked: boolean;
  evidence: number[];
  previous: boolean;
  carried_forward: boolean;
  report: { note: string; status: string | null; at: string | null } | null;
  /** The agenda point the action is about in this meeting (a previous action
   * stays grouped under the actions point: section_id). */
  topic_section_id?: string | null;
}

export interface MinuteNote {
  text: string;
  evidence: number[];
  at: string | null;
}

export type MinuteStatus = "notes" | "composing" | "composed" | "failed" | "edited";

export interface MinuteDto {
  id: string;
  kind: "section" | "opening" | "adjournment" | "voting_record" | "provenance" | string;
  section_id: string | null;
  notes: MinuteNote[];
  narrative_md: string | null;
  version: number;
  status: MinuteStatus;
  source_tier: "live" | "refined" | "mixed" | null;
  composed_at: string | null;
  locked: boolean;
  error: string | null;
}

export type AttendanceStatus = "present" | "represented" | "absent" | "excused" | "not_registered";

export interface AttendeeDto {
  id: string;
  /** Guests appear as an opaque per-session alias (`guest:~…`); null for a
   * public viewer. */
  person_key: string | null;
  name: string;
  username: string | null;
  email: string | null;
  status: AttendanceStatus;
  online: boolean;
  voting: boolean;
  represented_by: string | null;
  mandate_ref: string | null;
  opted_out: boolean;
  required_next: boolean;
  required_reason: string | null;
  talk_seconds: number;
}

export interface AttachmentDto {
  id: string;
  section_id: string | null;
  kind: "whiteboard" | "upload" | string;
  filename: string;
  caption: string | null;
  author: string | null;
  created_at: string | null;
  url: string;
}

/** Parse summary of an uploaded PDF (contract §2.1 / §3 update). */
export interface DocSummary {
  format: "om_report" | "generic";
  agenda_points: number | null;
  subpoints: number | null;
  decisions_to_take: number | null;
  open_actions: number | null;
  decisions: number | null;
  roster_members: number | null;
}

export interface DocumentDto {
  id: string;
  kind: "agenda" | "previous_notes";
  filename: string;
  title: string | null;
  status: "uploaded" | "parsing" | "done" | "failed";
  error: string | null;
  page_count: number | null;
  summary?: DocSummary | null;
}

export interface QuorumDto {
  required: number | null;
  voting_present: number;
  voting_total: number;
  met: boolean;
}

export interface SegmentDto {
  seq: number;
  identity: string | null;
  name: string | null;
  person_key: string | null;
  t_start: string | null;
  t_end: string | null;
  text: string;
  text_refined: string | null;
  tier: number;
  is_gap: boolean;
  gap_reason: string | null;
}

export interface Snapshot {
  v?: number;
  type?: "state";
  sid: string;
  version: number;
  session: SessionMeta;
  sections: SectionDto[];
  decisions: DecisionDto[];
  actions: ActionDto[];
  minutes: MinuteDto[];
  attendees: AttendeeDto[];
  attachments: AttachmentDto[];
  documents: DocumentDto[];
  quorum: QuorumDto | null;
}

export type CollectionKey = "sections" | "decisions" | "actions" | "minutes" | "attendees" | "attachments" | "documents";

export interface RemovedRef {
  kind: string;
  id: string;
}

export interface StateDelta {
  session?: Partial<SessionMeta>;
  sections?: SectionDto[];
  decisions?: DecisionDto[];
  actions?: ActionDto[];
  minutes?: MinuteDto[];
  attendees?: AttendeeDto[];
  attachments?: AttachmentDto[];
  documents?: DocumentDto[];
  quorum?: QuorumDto | null;
  removed?: RemovedRef[];
}

export type ActivationKind = "topic" | "decision" | "action" | "attendance";

export interface Activation {
  kind: ActivationKind;
  tab: "agenda" | "decisions" | "actions" | "attendance";
  section_id: string | null;
  item_id?: string | null;
  prio: number;
}

// ── Real-time messages (topic "meet-ai") ──────────────────────────────────

export interface CaptionMsg {
  v: 1;
  type: "caption";
  seq: number;
  identity: string;
  name: string;
  person_key: string | null;
  t_start: string | null;
  text: string;
  /** 2 when the Mac Studio transcribed it live (tier 2 first). */
  tier: 1 | 2;
}

export interface CaptionUpdateMsg {
  v: 1;
  type: "caption-update";
  seq: number;
  text: string;
  tier: 2;
}

export interface GapMsg {
  v: 1;
  type: "gap";
  seq: number;
  t_from: string | null;
  t_to: string | null;
  reason: string | null;
  name?: string | null;
}

export interface StateMsg {
  v: 1;
  type: "state";
  version: number;
  delta?: StateDelta;
  activations?: Activation[];
}

export interface PositionMsg {
  v: 1;
  type: "position";
  version: number;
  live_section_id: string | null;
  prev_section_id: string | null;
  by: "chair" | "ai";
  undo_until: string | null;
}

export interface AnnounceMsg {
  v: 1;
  type: "announce";
  aid: string;
  kind: "position" | "session" | "timebox";
  title: string;
  subtitle: string | null;
  audio_url: string | null;
}

export interface ProposalMsg {
  v: 1;
  type: "proposal";
  pid: string;
  to_section_id: string;
  title: string;
  reason: string;
  confidence: number;
}

export interface AgentMsg {
  v: 1;
  type: "agent";
  status: AgentStatusValue;
  backlog_s: number | null;
  speakers: AgentSpeaker[];
  tier2: Tier2Status;
}

export interface SessionMsg {
  v: 1;
  type: "session";
  state: "started" | "paused" | "resumed" | "ended" | "finalising" | "review" | "published";
  sid?: string;
}

/** The chair said "end this meeting": ask them to confirm (v3.2). */
export interface EndRequestMsg {
  v: 1;
  type: "end_request";
  heard: string;
  identity?: string;
}

export type MeetAiMessage =
  | EndRequestMsg
  | CaptionMsg
  | CaptionUpdateMsg
  | GapMsg
  | StateMsg
  | PositionMsg
  | AnnounceMsg
  | ProposalMsg
  | AgentMsg
  | SessionMsg;

// ── Ops (§3.1) ────────────────────────────────────────────────────────────

export type HumanOp = { op: string } & Record<string, unknown>;

export interface OpsResult {
  applied: number;
  rejected: Array<{ op: HumanOp; reason: string }>;
  version: number;
}

// ── Setup, series, review (§2.1, §2.3) ────────────────────────────────────

export interface SeriesDto {
  id: string;
  title: string | null;
  meeting_type: MeetingType;
  majority_rule: MajorityRule;
  quorum_required: number | null;
  [key: string]: unknown;
}

export interface RosterDto {
  id: string;
  person_key: string;
  display_name: string;
  username: string | null;
  email: string | null;
  voting: boolean;
  active: boolean;
}

export interface SessionListItem {
  id: string;
  status: SessionStatus;
  started_at: string | null;
  ended_at: string | null;
  published_at: string | null;
}

export interface Recipient {
  name: string;
  email: string;
}

export interface ReviewDraft {
  next_meeting: { date_iso?: string | null; duration_min?: number | null; room: "same" | "new" };
  next_agenda: Array<{ title: string; body?: string | null }>;
  recipients: { report: Recipient[]; invite: Recipient[] };
  distribution: { send_report: boolean; send_invites: boolean; attach_snapshots: boolean; include_transcript: boolean };
}

export interface ReviewFinal {
  summary?: string[] | string | null;
  next_agenda?: Array<{ title: string; body?: string | null }>;
  required_next?: Array<{ name: string; reason?: string | null; email?: string | null }>;
  verify?: string[];
}

export interface ReviewResponse {
  session: SessionMeta;
  jobs: Record<string, JobInfo> | null;
  review: ReviewDraft | null;
  final: ReviewFinal | null;
}

export interface OutputDto {
  id: string;
  kind: string;
  filename: string;
  download_url?: string;
  url?: string;
  [key: string]: unknown;
}
