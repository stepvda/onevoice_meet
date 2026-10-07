# Meet++ v3 — implementation contract

Source of truth for the Release 1.1 implementation of **FDD v3.1**
(`docs/MeetPlusPlus_FDD_v3.1.docx`). Every component implements against this
file. Section numbers "FDD §x" refer to the FDD.

- Data model: `meeting-api/app/meetpp/models.py` (schema v3, authoritative).
- Language: **English only** (spoken language, prompts, minutes, report, TTS).
  The UI stays localised via i18next (`meetpp` namespace).
- Person identity: `person_key` = `"sub:<user sub>"` for signed-in users
  (LiveKit identity `user-<sub>`), `"guest:<uuid>"` for anonymous guests
  (uuid generated once per browser, sessionStorage key `meetpp_guest_key`).
- All timestamps on the wire are ISO-8601 UTC strings (`...Z` or `+00:00`).

## 0. Components and file ownership

| Component | Owner paths | Notes |
|---|---|---|
| Backend core | `meeting-api/app/meetpp/*.py` (except `report/`), `meeting-api/app/meetpp/locales/`, `meeting-api/tests/meetpp/`, `meeting-api/app/main.py`, `meeting-api/app/config.py`, `meeting-api/app/webhooks.py`, `meeting-api/app/scheduler.py`, `meeting-api/requirements.txt`, `meeting-api/Dockerfile` | models.py already written |
| Report renderer | `meeting-api/app/meetpp/report/` (new), `meeting-api/tests/meetpp/test_report.py` | pure function of the export dict (§7) |
| Agent (tier 1) | `meetpp-agent/` | |
| Speech service (tier 2) | `meetpp-speech/` (new) | runs on the Mac Studio |
| Frontend | `frontend/src/components/meetpp/`, `frontend/src/lib/meetpp/`, `frontend/src/routes/MeetppReview.tsx`, integration edits in `PresenterSpotlight.tsx`, `Room.tsx`, `EgressLayoutPiP.tsx`, `Lobby.tsx`, `NotesWhiteboardPanel.tsx`, `CaptionsOverlay.tsx`, `frontend/public/locales/en.json` (meetpp keys only) | |
| Infra | `docker-compose.yml`, `.env.example`, `caddy/Caddyfile`, `DEPLOYMENT.md` | integrator |

Nobody commits, stashes, checks out or resets git state; the integrator does.

## 1. Meeting outline (sections)

Template **agenda**: `opening`, `previous_actions` (skipped when the series has
no open actions), agenda points (`agenda`, sub-points have `parent_id`), `aob`
(skipped when the last agenda point matches /any other (urgent )?(business|matter)|aob|a\.o\.b/i),
`new_actions`, `closing`.
Template **goal**: `opening`, `goal`, `deliverables`, `next_steps`, `planning`, `closing`.

Display numbers: agenda points are numbered `1..n` in order; sub-points
`n.m`. Fixed sections have no number. `section.status`:
`pending | live | done | deferred | skipped`. Exactly one section is `live`
while the session runs (`session.live_section_id`).

Navigation order for Next/Back = outline order of **top-level** sections
(sub-points are navigable only via "Move here"). Skipped sections are
passed over.

## 1a. Prefill from the setup PDFs (product requirement)

The two optional setup uploads (typically OneVoice OM meeting-report PDFs)
**prefill the items to cover on every tab before the meeting starts**; live
interpretation then updates those items instead of creating duplicates.

| Tab | Prefilled from | Items | Live update |
|---|---|---|---|
| Agenda | agenda PDF ("Agenda" section) | agenda points (body) + sub-points (a)(b)… as a checklist (section rows, status `pending`) | sub-point → `done` when the topic moves past it or its parent closes; point → `live`/`done` by position |
| Decisions | agenda PDF: motions and questions for decision inside points ("Shall we …?", "Resolved that …?", "Question for the board: …", "We can vote on it …", "Confirming: …", "Suggestion: …?") | decisions with **status `pending`** (= to decide), origin `pdf`, section = that point, title + draft resolution ("that …") | AI `decision.update {ref, status:"adopted"|"rejected"|"withdrawn", resolution, how_taken, vote}`; the tick prompt lists pending decisions of the live/topic sections with refs and forbids re-adding them; duplicates of a pending decision are merged into it |
| Actions | previous-notes PDF ("Follow-up actions") | open actions (status `open`, `previous:true`) shown under the Previous actions section as **To review** until a report exists | AI `action.update {ref, report_note, status…}` |
| Attendance | previous-notes PDF ("Attendance") | roster members (expected; status `not_registered` until they join, `absent` at finalisation if they never join) | presence + `attendance.set` |
| Papers | both uploads | the PDFs themselves | — |

`DecisionDto.status` therefore also takes **`pending`**. Pending decisions that
were never taken are left out of the report's "Decisions" section (the
section's minutes say "No resolution was put."). `DocSummary` gains
`decisions_to_take:int`.

## 2. REST API (all under `/api/v1`)

Auth legend: **chair** = user JWT (`Authorization: Bearer`) and
owner/co-host (`is_moderator`) → 404 otherwise; **room** = LiveKit room token
in `X-Meet-Room-Token` for the session's room (any participant; egress and
public-viewer tokens are read-only); **room-chair** = room token whose
identity is a chair or a designated editor; **internal** = HMAC
(`X-Meetpp-Timestamp`, `X-Meetpp-Signature`, signing v2 — see §12; ±60 s, a repeated signature is refused).

### 2.1 Setup and lifecycle (chair)
- `POST /meetings/{meeting_id}/meetpp/sessions` body `{template:"agenda"|"goal", mode:"lead"|"assist"="lead", series_id?:str, meeting_type?:"informal"|"board"|"general_assembly", goal?:str}` → `201 {session: SessionMeta, series: Series, imported_actions: [ActionDto]}`; creates the fixed sections immediately (status `setup`).
- `GET /meetings/{meeting_id}/meetpp/sessions` → `{sessions:[{id,status,started_at,ended_at,published_at}]}`
- `PATCH /meetpp/sessions/{sid}` body any of `{mode, goal, editors:[identity], settings:{show_public,in_recordings,timebox_nudges,speak}}` → `SessionMeta`
- `PUT /meetpp/sessions/{sid}/outline` (setup or live) body `{agenda:[{id?, title, body?, presenter?, timebox_minutes?, subpoints:[{id?, title, body?}]}]}` → replaces the agenda points (fixed sections untouched) → `{sections:[SectionDto]}`
- `POST /meetpp/sessions/{sid}/documents` multipart `file`, `kind=agenda|previous_notes` → `202 {document: DocumentDto}`; parsing in background; on success agenda → outline replaced (setup only), previous_notes → series actions imported (status open, origin pdf) + document listed as paper.
- `GET /meetpp/sessions/{sid}/documents/{doc_id}` → `{document: DocumentDto, summary: DocSummary|null, structured?: {...}}`
  - `DocSummary` = `{format:"om_report"|"generic", agenda_points:int, subpoints:int, decisions_to_take:int, open_actions:int, decisions:int, roster_members:int}`
  - Both upload kinds accept the OneVoice OM **meeting report** PDF (the typical input, e.g. the previous meeting's report or an agenda in the same layout): kind=agenda imports its "Agenda" section (numbered points, description, inline sub-points (a)(b)…); kind=previous_notes imports open "Follow-up actions" as series actions, previous decisions for reference, and seeds the series roster from its "Attendance" table when the roster is empty (members voting=True; `person_key` "name:<normalised name>" until matched to a participant).
- `POST /meetpp/sessions/{sid}/start` → `SessionMeta` (status running; first section live; agent started)
- `POST /meetpp/sessions/{sid}/pause` · `/resume` → `SessionMeta`
- `POST /meetpp/sessions/{sid}/end` → `{status:"finalising"}` (returns immediately; jobs run in background)
- `POST /meetpp/sessions/{sid}/finalise` → re-runs failed finalisation jobs → `{jobs}`
- `DELETE /meetpp/sessions/{sid}` (owner only)

### 2.2 Live (room / room-chair)
- `GET /meetpp/rooms/{room}/active` (no auth) → `{active:bool, sid?, provider_label?, consent_version:"v3"}`
- `GET /meetpp/sessions/{sid}/state` (room) → **Snapshot** (§3)
- `GET /meetpp/sessions/{sid}/transcript?after=<seq>&limit=<≤1000, default 500>` (room) → `{segments:[SegmentDto], next_after:int|null}` (client pages until `next_after` is null)
- `POST /meetpp/sessions/{sid}/consent` (room) body `{decision:"accept"|"opt_out", person_key:str, name?:str}` → `{ok:true}`. Client posts on **every** join/reconnect. Server maps the caller's identity → person_key.
- `POST /meetpp/sessions/{sid}/position` (room-chair; chair/co-hosts only, not editors) body `{action:"next"|"back"|"move", section_id?:str}` → `{live_section_id, version}`
- `POST /meetpp/sessions/{sid}/position/undo` (room-chair) → `{live_section_id, version}`; 409 if nothing to undo.
- `POST /meetpp/sessions/{sid}/proposal` (room-chair) body `{pid, accept:bool}` → `{ok}`
- `POST /meetpp/sessions/{sid}/ops` (room-chair: chair or editor) body `{ops:[HumanOp]}` → `{applied:int, rejected:[{op,reason}], version}`
- `POST /meetpp/sessions/{sid}/sections/{section_id}/compose` (room-chair) → `{status:"composing"}`
- `POST /meetpp/sessions/{sid}/attachments` (room; multipart `file` PNG ≤5 MB, `caption?`, `section_id?`) → `{attachment: AttachmentDto}`
- `GET /meetpp/sessions/{sid}/attachments/{id}` (room or chair) → file
- `PATCH /meetpp/sessions/{sid}/attachments/{id}` body `{caption}` · `DELETE …` (room-chair)
- `PATCH /meetpp/sessions/{sid}/attendees/{attendee_id}` (room-chair) body any of `{status, voting, represented_by, mandate_ref, display_name, email, required_next, required_reason}` → `AttendeeDto`
- `GET /meetpp/tts/{hash}.ogg` (no auth, hash = sha256 hex of `voice|text`) → audio clip from `MEETPP_DATA_DIR/tts/`

### 2.3 Review and outputs (chair)
- `GET /meetpp/sessions/{sid}/review` → `{session: SessionMeta, jobs, review: ReviewDraft, final: {summary, next_agenda[], required_next[], verify[]}}`
- `PUT /meetpp/sessions/{sid}/review` body `ReviewDraft` = `{next_meeting:{date_iso?, duration_min?, room:"same"|"new"}, next_agenda:[{title, body?}], recipients:{report:[{name,email}], invite:[{name,email}]}, distribution:{send_report:bool, send_invites:bool, attach_snapshots:bool, include_transcript:bool}}`
- `PUT /meetpp/decisions/{decision_id}/vote` body `{method, for?, against?, abstain?, ballots?:[{name, person_key?, choice, cast_by?, proxy?}], confirmed?:bool}` → `DecisionDto` (quorum/eligible recomputed server-side)
- `PUT /meetpp/series/{series_id}/rules` body `{meeting_type?, majority_rule?, quorum_required?:int|null}` → `Series`
- `GET /meetpp/series/{series_id}/roster` → `{roster:[RosterDto]}` · `PATCH /meetpp/series/{series_id}/roster/{roster_id}` body `{voting?, active?, display_name?}`
- `GET /meetpp/sessions/{sid}/report.pdf` → live render of the meeting report (preview)
- `POST /meetpp/sessions/{sid}/publish` → `{published_at, outputs:[OutputDto], email_results:[...]}`
- `GET /meetpp/sessions/{sid}/outputs` · `GET /meetpp/sessions/{sid}/outputs/{oid}`
- `GET /meetpp/sessions/{sid}/export.json` (§7 shape) · `GET …/export.md` (minutes markdown)
- `GET /admin/meetpp/status` (platform admin), `POST /admin/meetpp/sessions/{sid}/end`

### 2.4 Internal (agent → meeting-api, HMAC)
- `POST /internal/meetpp/sessions/{sid}/segments` body
  `{segments:[{utterance_id, identity, name, t_start, t_end, text, lang, avg_logprob, no_speech_prob, tier?:2}],
    refinements:[{utterance_id, text, final:bool}],
    gaps:[{identity?, name?, t_from, t_to, reason}]}` → `{ok, seqs:{utterance_id: seq}}`.
  All three arrays optional. Server assigns `seq`; refinements set `text_refined`/`tier=2`
  and broadcast `caption-update`; `final:true` refinements come from the post-meeting pass. A segment with `tier:2`
  was transcribed live by tier 2 (§12): stored with `text_refined = text`, `tier=2`, captioned with `tier:2`.
  Gap reasons: `overloaded`, `agent_reconnect`, `consumer_restart`, `liveness`; meeting-api adds `agent_restart` for agent downtime it detects itself.
- `POST /internal/meetpp/sessions/{sid}/presence` body `{events:[{identity, name, kind:"standard"|..., event:"connected"|"disconnected", at}]}`
- `POST /internal/meetpp/sessions/{sid}/agent-status` body `{status:"listening"|"behind"|"reconnecting"|"paused"|"offline", backlog_s, rtf_p50?, speakers:[{identity, name, ok:bool}], tier2:"up"|"down"|"off", final_pass?:"running"|"done"|"failed"|"skipped"}` (`skipped` = tier 2 not configured or unavailable)
- `POST /internal/meetpp/sessions/{sid}/agent-token` body `{}` → `{token, ws_url}` (fresh hidden subscribe-only token, identity `meetpp-scribe-<sid>`, TTL 12 h)

## 3. State snapshot and DTOs

`GET /state` returns:
```json
{
  "v": 1, "type": "state", "sid": "…", "version": 12,
  "session": SessionMeta,
  "sections": [SectionDto], "decisions": [DecisionDto], "actions": [ActionDto],
  "minutes": [MinuteDto], "attendees": [AttendeeDto],
  "attachments": [AttachmentDto], "documents": [DocumentDto],
  "quorum": {"required": 2, "voting_present": 2, "voting_total": 3, "met": true} | null
}
```
- `SessionMeta` = `{id, status, template, mode, language, goal, meeting_type, majority_rule, series_id, series_title, meeting_id, room, live_section_id, topic_section_id, started_at, ended_at, published_at, settings:{show_public, in_recordings, timebox_nudges, speak}, editors:[identity], undo:{from, to, until}|null, proposal:{pid, to, reason, confidence}|null, agent:{status, backlog_s, speakers:[{name, ok}], tier2}, ai:{status:"ok"|"paused"|"unavailable"}, jobs:{...}|null}`
- `SectionDto` = `{id, kind, parent_id, position, number:"3"|"3.1"|null, title, body, presenter, timebox_minutes, status, started_at, ended_at, elapsed_seconds, source, locked, counts:{decisions, actions}}`
- `DecisionDto` = `{id, ref, section_id, title, resolution, how_taken, status, decided_at, origin, confirmed, locked, evidence:[seq], previous:bool, vote: VoteDto|null}`
- `VoteDto` = `{method, for, against, abstain, eligible, present, quorum_required, quorum_met, result, outcome_note, confirmed, ballots:[{name, person_key, choice, cast_by, proxy}]}`
- `ActionDto` = `{id, ref, section_id, title, description, assignees:[{name, person_key}], due, status, decision_ref, completed_at, completion_note, progress_notes, origin, locked, evidence:[seq], previous:bool, carried_forward:bool, report:{note, status, at}|null}` — `previous` = raised in an earlier session of the series.
- `MinuteDto` = `{id, kind, section_id, notes:[{text, evidence, at}], narrative_md, version, status, source_tier, composed_at, locked, error}`
- `AttendeeDto` = `{id, person_key, name, username, email, status, online, voting, represented_by, mandate_ref, opted_out, required_next, required_reason, talk_seconds}`
- `AttachmentDto` = `{id, section_id, kind, filename, caption, author, created_at, url}` (url = API path)
- `DocumentDto` = `{id, kind, filename, title, status, error, page_count, summary: DocSummary|null}`
- `SegmentDto` = `{seq, identity, name, person_key, t_start, t_end, text, text_refined, tier, is_gap, gap_reason}`

### 3.1 Human ops (`POST /ops`)
Same vocabulary as the AI ops (§5) plus:
`{"op":"decision.confirm","id"}`, `{"op":"decision.reject","id"}`, `{"op":"decision.update","id", title?, resolution?, how_taken?, status?, section_id?}`,
`{"op":"action.confirm","id"}`, `{"op":"action.reject","id"}`, `{"op":"action.update","id", title?, description?, assignees?:[name], due?, status?, report_note?, progress_note?, completion_note?}`,
`{"op":"decision.add", section_id, title, resolution?}` and `{"op":"action.add", section_id, title, assignees?, due?}` (manual adds),
`{"op":"minutes.edit","section_id"|"kind", narrative_md}` (locks the part),
`{"op":"section.add", parent_id?, title, kind:"agenda"|"aob"}`, `{"op":"section.update", id, title?, body?, presenter?, timebox_minutes?}`, `{"op":"section.status", id, status:"done"|"deferred"}`.
Human ops use ids (not refs). Any human edit sets `locked=true` on the item; AI ops on locked items are stored as `suggested`.

## 4. Real-time messages (LiveKit data, topic `meet-ai`, RELIABLE, server-published)

Envelope `{"v":1,"type":…}`. Payload ≤ 8 KB (otherwise send without `delta`; clients refetch).
- `caption` `{seq, identity, name, person_key, t_start, text, tier:1|2}`
- `caption-update` `{seq, text, tier:2}`
- `gap` `{seq, t_from, t_to, reason, name?}`
- `state` `{version, delta?:{session?:Partial<SessionMeta>, sections?:[], decisions?:[], actions?:[], minutes?:[], attendees?:[], attachments?:[], documents?:[], quorum?:{}|null, removed?:[{kind, id}]}, activations:[Activation]}`
  - Client rule: if `version == local+1` and `delta` present → merge by id (and apply `removed`); if `version <= local` → ignore; else → `GET /state` (full).
  - `Activation` = `{kind:"topic"|"decision"|"action"|"attendance", tab:"agenda"|"decisions"|"actions"|"attendance", section_id, item_id?, prio}` with prio topic 3, decision 5, action 4, attendance 2. Minutes never activate. Server sends ≤ 3 per message, highest prio first.
- `position` `{version, live_section_id, prev_section_id, by:"chair"|"ai", undo_until:iso|null}`
- `announce` `{aid, kind:"position"|"session"|"timebox", title, subtitle, audio_url:str|null}` (audio_url = `/api/v1/meetpp/tts/<hash>.ogg`; clients fall back to `speechSynthesis` when null or failing; egress page plays it)
- `proposal` `{pid, to_section_id, title, reason, confidence}` (destination: chair + co-hosts)
- `agent` `{status, backlog_s, speakers:[{name, ok}], tier2}`
- `session` `{state:"started"|"paused"|"resumed"|"ended"|"finalising"|"review"|"published"}`

## 5. Interpretation contract (LLM tick output, FDD Appendix A)

Prompt ids: sections are given as `S1..Sn` (outline order incl. sub-points),
actions as their refs `A-n`, decisions `D-n`, transcript lines as `[seq]`
(CONTEXT lines are marked). Output JSON:
```json
{"topic":{"section":"S5","confidence":0.86},
 "advance":{"to":"S6","confidence":0.9,"reason":"…"}|null,
 "ops":[…], "notes":[{"section":"S5","text":"…","evidence":[231]}]}
```
Ops: `decision.add {section, title, resolution, how_taken, status, decided_at_seq, vote?:{method, for, against, abstain, ballots?:[{person, choice}]}, evidence}`,
`decision.update {ref, title?, resolution?, how_taken?, status?, vote?, evidence}`,
`action.add {section, title, description?, assignees:[name], due?, from_decision?:ref, evidence}`,
`action.update {ref, status?, report_note?, progress_note?, completion_note?, due?, assignees?, evidence}`,
`attendance.set {person, status:"present"|"represented"|"absent"|"excused", represented_by?, evidence}`,
`attendance.require_next {person, reason, evidence}`,
`section.add {kind:"subpoint"|"aob", parent?, title}`,
`next_meeting.propose {when_text, iso?, evidence}`.
Validation: unknown fields ignored; long text truncated; unknown section → topic
section → live section; evidence: 1–10 seqs inside window ∪ context with ≥1
new; duplicates checked within the session (Jaccard ≥ 0.85); every rejection
stored in `meetpp_ops` with raw payload + reason.

## 6. Agent and speech service

### 6.1 meeting-api → meetpp-agent (`MEETPP_AGENT_URL`, Docker-internal, no auth)
- `POST /sessions` `{session_id, room, ws_url, token, language:"en", glossary:str, accepted_identities:[identity], paused:bool}` → 201
- `PATCH /sessions/{sid}` any of `{accepted_identities, paused, glossary}` → 200 (idempotent; meeting-api re-sends the full accepted list every 20 s)
- `POST /sessions/{sid}/finalize` → 202; agent stops capture, drains its queue, runs the tier-2 final pass over the stored utterances (in order, per speaker, with previous-text context), posts `refinements[final=true]`, then posts agent-status `final_pass:"done"|"failed"`.
- `DELETE /sessions/{sid}` → 204 (leave the room, no final pass)
- `POST /tts` `{text, voice:"am_michael"}` → `{hash, path}` (path is the absolute container path; clip written to `MEETPP_DATA_DIR/tts/<hash>.ogg`, proxied to meetpp-speech; 503 if unavailable)
- `GET /health` → `{ok, sessions:[{sid, connected, consumers, backlog_s, paused, tier2}], model, vad:"silero", rtf_p50, tier2:"up"|"down"|"off"}`

Audio store: `MEETPP_DATA_DIR/<sid>/audio/<utterance_id>.ogg` (Opus 24 kbit/s) +
`index.jsonl` (`{utterance_id, identity, name, t_start, t_end, duration_s, text}` per line; text = tier-1 text). The
directory is a shared volume between agent and meeting-api; meeting-api
deletes `<sid>/audio/` at publish and the retention job after 7 days.

### 6.2 meetpp-agent → meetpp-speech (`MEETPP_SPEECH_URL`, e.g. `http://10.88.0.2:9310`)
Auth: `X-Meetpp-Timestamp`, `X-Meetpp-Signature`, signing v2 with `MEETPP_SPEECH_SECRET` (§12), ±60 s, replays refused.
- `POST /transcribe?language=en&prompt=<urlencoded ≤ 600 chars>` body = audio bytes (`audio/ogg` Opus or `audio/wav`) → `{text, avg_logprob, duration_s, rtf, model, repetition:bool}`
- `POST /tts` JSON `{text, voice:"am_michael", format:"ogg"}` → `audio/ogg` bytes
- `GET /health` → `{ok, model:"large-v3-turbo", tts:"kokoro", queue, busy, rtf_p50}`

## 7. Export shape (input of the report renderer)

`app.meetpp.export.build_export(db, session) -> dict` returns:
```json
{
 "org": {"name","tagline","seat","enterprise","rpr","email","website","iban","logo_path":null,"placeholders":true},
 "meeting": {"title","series_title","date_iso","location","type_label":"Meeting"|"Board meeting"|"General assembly",
             "status_label":"Held","convened_at_iso","adjourned_at_iso","quorum_note","formal":bool},
 "generated_at_iso": "…",
 "attendance": {"summary":{"present","represented","absent","excused","not_registered"},
                "rows":[{"name","username","status_label","represented_by","mandate"}]},
 "agenda": [{"number":"1","title","body","subpoints":[{"label":"a","title","body"}]}],
 "decisions": [{"number":1,"ref":"D-1","title","resolution","how_taken","status_label":"Adopted","decided_at_iso","agenda_number":"3",
   "vote": {"question","for","against","abstain","result_label":"Adopted","voting_body_label":"Board","majority_label":"Simple majority",
            "eligible","basis_label":"Directors in office","present_or_represented","quorum_required","quorum_met",
            "outcome_sentence","ballots":[{"name","vote_label":"For","cast_by","proxy":false}]} | null}],
 "actions": [{"number":1,"ref":"A-3","title","carried_forward":bool,"description","status_label":"Open","assignees":["…"],
              "due_iso","completed_iso","reported_note","progress_notes","completion_note",
              "also_on":[{"title","date_iso"}],"from_decision":"…"|null}],
 "papers": [{"title","file","agenda_number":null}],
 "minutes": {"saved_at_iso","version","markdown"},
 "next_agenda": [{"number","title","body"}],
 "next_meeting": {"date_iso","location"} | null
}
```
`app.meetpp.report.render_meeting_report(export: dict) -> bytes` (PDF) and
`app.meetpp.report.render_agenda(export: dict) -> bytes` (next-meeting agenda:
identity block, meeting table for `next_meeting`, `next_agenda`, open actions
carried forward). Both pure and deterministic; ReportLab only.

## 8. Configuration (new or changed)

meeting-api: `MEETPP_SPEECH_ENABLED` (bool, info only), `MEETPP_AUDIO_RETENTION_DAYS=7`,
`MEETPP_TICK_SECONDS=12`, `MEETPP_CONTEXT_SECONDS=90`, `MEETPP_MAX_TOKENS_PER_HOUR=3000000`,
`MEETPP_LANGUAGES=["en"]`, `MEETPP_TTS_VOICE=am_michael`.
meetpp-agent: `MEETPP_SPEECH_URL`, `MEETPP_SPEECH_SECRET`, `MEETPP_DATA_DIR=/var/lib/meet/meetpp`,
`STT_MODEL=small`, `STT_DEGRADE_MODEL=base`, `STT_THREADS=2`, `LIVEKIT_WS_URL_INTERNAL`, `MEETING_API_URL`, `MEETPP_INTERNAL_SECRET`.
meetpp-speech: `MEETPP_SPEECH_SECRET`, `MEETPP_SPEECH_BIND=10.88.0.2`, `MEETPP_SPEECH_PORT=9310`,
`MEETPP_SPEECH_MODEL=mlx-community/whisper-large-v3-turbo`, `KOKORO_MODEL`, `KOKORO_VOICES`.
meeting-api also: `MEETPP_AGENT_WS_URL=ws://host.docker.internal:7880` (URL handed to the agent),
`MEETPP_FINAL_PASS_TIMEOUT_SECONDS=300`. Removed: `MEETPP_SPEAKER_ALIASING`, `TTS_VOICES`, `STT_REMOTE_URL`.

## 9. Deviations and clarifications (backend core, Release 1.1)

Additive unless stated; frontend-visible shapes above are unchanged.

**Auth**
- `/state`, `/transcript`, `/ops`, `/sections/{id}/compose`, `/attendees/{id}`, `/attachments` (POST, GET, PATCH, DELETE)
  accept a room token **or** the chair's user JWT (`Authorization: Bearer`, owner/co-host via `is_moderator`;
  404 otherwise). A chair JWT has chair and edit rights. `/consent` needs a room token.
- `AttendeeDto.email` is `null` in room-token responses and in broadcast deltas (privacy); it is filled in
  chair-JWT responses (`/state`, `/review`) and in the `PATCH /attendees/{id}` response.
- `/position`, `/position/undo` **and `/proposal`** are chair/co-host only (not editors): accepting a proposal moves the meeting.
- Editors are `user-<sub>` identities only; other values in `PATCH {editors}` are dropped.

**Snapshot / DTOs**
- `SessionMeta` adds `quorum_required` (int|null, the series rule; null = majority of the voting roster) and
  `voting_body` (`"Board"` | `"General assembly"` | null for informal meetings).
- `SectionDto.elapsed_seconds` = accumulated **finished** live runs; `started_at` = start of the **current** live run
  (null while the session is paused); `ended_at` = end of the last run. Clients add `now − started_at` while live.
- `DecisionDto.previous` is always false (earlier decisions are not part of a session's state).
- `ActionDto.previous` is also true for actions imported from the previous-notes PDF (origin `pdf`); previous actions are
  grouped under the `previous_actions` section (`section_id`). AI-recorded actions start as `proposed`;
  `action.confirm` or publishing turns them `open`.
- `DocumentDto.title`: "Report of <meeting> (<date>)" / "Agenda — <first line>".

**Real-time**
- Every message envelope also carries `sid`.
- On every position change the server sends `position` and then `state` **with the same version**; that `state`
  delta contains the changed sections and the full `session` (with `live_section_id`).
- Ending a session sends `session {state:"ended"}` then `{state:"finalising"}`; finalisation ends with `{state:"review"}`.
- Timebox nudges are `announce {kind:"timebox"}` sent to the chair and co-hosts only, without TTS.
- `ai.status`: `unavailable` (no LLM configured), `paused` (tick breaker open or the last tick failed), `ok`.

**REST**
- `GET /review` also returns `state` (full chair-scoped snapshot) and `final.next_meeting_proposal`
  (`{when_text, iso}` from `next_meeting.propose`, or null).
- `POST /publish` → `{published_at, version, outputs:[OutputDto], email_results:[{kind:"report"|"invite", email, ok}], next_meeting_id}`;
  `GET /outputs` → `{outputs:[OutputDto]}`, `OutputDto = {id, kind:"report_pdf"|"agenda_pdf"|"ics"|"email", version, filename, created_at, url|null, recipients, results}`.
- `Series = {id, title, meeting_id, meeting_type, majority_rule, quorum_required, quorum_default, voting_members}`;
  `RosterDto = {id, person_key, name, display_name, username, email, voting, active, first_seen_at, last_seen_at}`.
  Toggling `voting` on a roster member or an attendee updates both (sessions not yet published).
- `GET /export.md` returns `text/markdown`. `GET /report.pdf` and `POST /publish` answer 503 when the report
  renderer cannot be imported.
- `POST /internal/.../agent-token` → 404 when the session is not running, paused or finalising.
- `POST /internal/.../segments` → `seqs` maps only stored utterances (and refined ones); segments dropped for
  missing consent or as an announcement echo are absent. Re-posting an utterance is idempotent.
- Human ops: `decision.add` defaults to `status:"adopted"` and `confirmed:true`; `decision.reject` / `action.reject` delete
  the record (delta `removed`), a previous action cannot be rejected (set `status:"cancelled"`); `section.status` on the
  live section is refused (use Next). AI ops on a connected person with `attendance.set absent|excused` are refused.

**Export (§7)**
- `org.placeholders` is the list of placeholder fields (`["enterprise","iban"]`, truthy) or `false`.
- `actions[].also_on[]` entries carry `raised:true` on the meeting where the action was raised; pending decisions
  never taken are left out of `decisions`; `vote` is null for informal meetings.

**Interpretation and minutes**
- Prompt ids `S1…Sn` are pinned to the outline the LLM saw for the whole tick (a section added meanwhile cannot shift them).
- A `decision.add` matching a pending decision of the same top-level section (Jaccard ≥ 0.6 on the title or on
  title + resolution) updates that decision instead; other duplicates (≥ 0.85) are refused, or applied as a status update
  when the status differs.
- Section composition: the 50-word minimum applies only when the section transcript has ≥ 200 words; a missing
  `> **RESOLVED:**` block for an adopted decision is appended rather than failing the draft; "No resolution was put." is
  appended when nothing was adopted. A non-forced composition finishing after the section was reopened stays a draft.
- Final composition: the LLM writes opening, adjournment, summary, next agenda and items to verify (deterministic
  fallbacks); the record of voting and the provenance are generated from the data.
- Finalisation jobs: `tier2`, `compose`, `final` (marks absentees, then composes), `render` (test render of the report).
  Agent `final_pass:"skipped"` → `tier2` skipped (minutes on the live-quality transcript). meeting-api posts an
  `agent_restart` gap when it restarts a session missing from the agent's `/health`.

**Data model** (additive, nullable; added to existing v3 databases by `ensure_schema()`)
- `meetpp_documents.summary_json` (DocSummary); `meetpp_segments.section_id` (live section at ingest, re-tagged to the
  detected topic by the tick; used to compose a section from its own transcript).
- Seeded roster members (from the previous report) use `person_key = "name:<normalised name>"` until matched to a
  joining participant by username or normalised display name.

## 10. Changes from the meeting-240 replay (Release 1.2)

Found by replaying the 4 October 2026 meeting (91 min, agenda v4 and a previous report) through the pipeline with
the real LLM (`meeting-api/tools/meetpp_replay.py`).

**Speech**
- Decode budget on both tiers: `max_new_tokens` (faster-whisper) / `sample_len` (mlx-whisper) =
  `16 + 6 × audio seconds`, capped at 200 (tier 1) / 224 (tier 2). A looping decode (". . . ." or a repeated phrase)
  otherwise runs to Whisper's token limit at each of the three fallback temperatures and stalls the queue.

**Prefill**
- Decisions to take: the LLM's list is completed with the deterministic cues ("Shall we …?", "Resolved that …",
  "Question for the board: …", "Confirming: …", "Suggestion: …?", approval points) for every point or sub-point the LLM
  left out; the LLM wins where both exist. Points asking only for a report or a confirmation of fact are not decisions.
- Short titles name the subject the meeting deals with, not the background of the point.

**Interpretation**
- The tick prompt shows the agenda text of the live point **and of the next point** (marked `← NEXT`).
- Previous actions show their description while TO REVIEW, and the recorded report once reported, so a wrong report
  can be corrected. Rules added: done only when that action is said to be finished (a general "that closes off the
  actions" does not cover a named exception); `action.add` of work already listed becomes `action.update`; every
  update of a previous action carries a `report_note`.
- An AI `action.update` that changes nothing is refused as `no change` (no activation).
- Topic-based move (no sure `advance`): two consecutive sure ticks (≥ 0.85) or 3 of the last 4 ticks (≥ 0.6) on the
  same later point move the meeting **only when a cue for that point was heard** — an `advance` to it of ≥ 0.5 in the
  last 150 s ("number four", "on to the budget"). Without a cue the talk must stay on that later point for 25
  consecutive ticks (≈ 5 min: the chair moved on without saying so). This stops a director previewing the next
  point's subject from moving the meeting (seen in the replay: a member previewing the next point's subject). Topic state keeps
  `later_hist`, `later_run` and `hint`; a move clears them.
- The live point's agenda text is shown up to 6,000 characters per (sub-)point (the next point's up to 400), so that
  a phrase from one of its later sub-points is not taken for the next point. (Holding back an `advance` whose own
  topic was still the live point was tried and dropped: the model keeps the topic on the live point after a real
  transition, so the move came minutes late.)

**Minutes**
- Section length target = ⅓ of the section's transcript words, 80–1,500 (prompt `LENGTH: about N words`). A draft
  over 1.5 × the target (and 150 words over) is condensed in two calls (`compose_condense`): (1) at most
  target / 35 key points, one sentence each, tagged `[n.m]` with their sub-point; (2) minutes written from those points
  alone, under the draft's `### n.m` sub-headings, without RESOLVED blocks (finish_markdown adds them back). The result
  must be shorter than the draft and at least half the target, else the draft stands; only drafts over 6,000 words are
  refused. Asked to shorten the draft itself, the model copied it (meeting replay: 3,397 → 3,397 words); the two-call
  form gave 3,397 → 696, 2,082 → 951 and 916 → 476 for targets of 1,100, 950 and 480.

**Votes and roster**
- An adopted decision with no vote in a board or general-assembly meeting is recorded as taken by assent (ballots:
  the voting members present, "for"). An assent sent with zero counts is counted from those ballots; votes in favour
  are capped at the voting members present or represented.
- `meetpp_roster.first_session_id` (nullable; null = seeded from a previous report). A signed-in participant votes by
  default only while the series has no voting member from before this meeting — so everyone signed in votes in a
  series' first meeting (previously only the first to join). Composition `max_tokens` 6,000 (a 2,300-word draft was truncated at 3,000).

## 11. Meet++ board as a stream window (Release 1.2)

The board is no longer a stage replacement with a mini tile and a drawer: it is a stream window like a camera or a
screen share, and takes part in the room layout (frontend `lib/stage.ts`, backend `app/stage.py`).

- **Stage keys** (`presenter_identity` in the room metadata): a camera = the identity (`playback` for the playback
  ingress), a screen share = `<identity>#screen`, the board = `meetpp:board`. `POST /meetings/{id}/presenter` accepts
  any of them (host and co-hosts); `POST /meetpp/sessions/{sid}/board-to-main` presents the board.
- **Main stream** (live, recording and livestream alike): presenter (when on the stage) > screen share > playback >
  board > active speaker > any camera. An explicit presenter now wins over a screen share.
- **Layouts:** grid shows the board as a cell (full board when ≥ 560 px wide, else a compact summary); speaker shows
  the main stream plus a strip of every other stream window (the board as a compact summary); single-speaker shows the
  main stream. A grid gives way to speaker while a screen is shared or while the main stream is the board or a screen
  share (a presented camera keeps the grid).
- **Server moves** (`app/stage.py`, all writes through `app/room_metadata.patch_room_metadata`, serialised per room):
  a Meet++ session starting makes the board the presenter (during a screen share the board follows it); a screen
  share or the playback starting while a presenter is set takes the stage and keeps the previous presenter in
  `presenter_prev`; it stopping (or its publisher leaving) brings that presenter back; the session ending takes the
  board off the stage; the host choosing a presenter clears `presenter_prev`. Webhooks: `track_published`,
  `track_unpublished`, `participant_left`.
- **Per viewer** (never sent anywhere, reset on leaving): "My view" menu at the top of the stage (room layout / grid /
  speaker / single speaker) and zoom of any stream window to the whole stage (double-click or the tile's zoom button;
  "Open the board" in the Meet++ menu zooms the board). Tiles carry "Present to everyone" for the host and co-hosts and
  a "Presenter" badge.
- Store: `stageTakeover` and `drawerOpen` are gone; `boardOnStage` (this viewer sees the full board) and
  `boardPresenter`. The board's keyboard shortcuts act only while `boardOnStage` (chair Next/Back always).

## 12. Review fixes, tier 2 first and the adoption gate (Release 1.2.1)

- **Signing v2** (agent → meeting-api and agent → meetpp-speech): `X-Meetpp-Signature =
  hex(HMAC_SHA256(secret, "v2\n" + ts + "\n" + METHOD + "\n" + target + "\n" + hex(sha256(body))))`, where
  `target` is the raw path plus `?query` when there is one. ±60 s; an accepted signature is remembered for the window
  and a repeat gets 401, so every retry is re-signed. Test vector: secret `test-secret`, ts `1700000000`, `POST`,
  target `/api/v1/internal/meetpp/sessions/01HZZZZZZZZZZZZZZZZZZZZZZZ/segments?x=1`, body `{"a":1}` →
  `572404eb590905ef095aea5d1c8bfafbcd49bf92e158be425640ccdd3a868582`. meeting-api, meetpp-agent and meetpp-speech
  are deployed together.
- **Tier 2 first** (agent, `MEETPP_TIER2_FIRST`, default on): while tier 2 is up every utterance is stored and sent to
  meetpp-speech first (budget 4 s); its text is posted as the segment with `tier:2` and is the live caption. Tier 1
  decodes the utterance only when tier 2 is busy (503), fails, exceeds the budget or answers with a degenerate text
  (repetition, compression, too long, empty); such an utterance gets no near-live refinement (the final pass covers
  it). Segments are posted in speaking order. Whole-utterance hallucinations (tier-1 blocklist) are dropped. With tier
  2 down or `MEETPP_TIER2_FIRST=0`, tier 1 is live and tier 2 refines, as before.
- **Adoption gate** (`app/meetpp/agreement.py`): an AI op may set a decision to adopted (or adopt it through a vote)
  only when the lines it cites show agreement: a declared outcome from anyone ("agreed", "unanimously approved", "we
  all agree", "carried", "no objection", "the resolution is", "decided"), or a response that answers an earlier cited
  line by another speaker ("that's a good idea", "let's go with that", "that's okay", "I like that", "I agree", or a
  reply that is nothing but assent: "Yes.", "To all? Yeah."). Negated or asked phrases do not count. Otherwise the
  decision is (or stays) proposed and the model's vote is not applied; the next tick sees it proposed and re-cites.
  The cited lines (with the 3 lines before each) must also share a content word with the decision's title or
  resolution, so agreement to another proposal cannot adopt it.
  People are not gated. Prompt v3.3 asks the model to cite the agreeing line.
- **Guests in room data** appear as an opaque per-session alias `guest:~<20 hex>` (state, deltas, captions,
  transcript, ballots, assignees); a chair's ballot that sends the alias back is mapped to the real key. Public
  viewers get `/state` with `person_key: null` and no `/transcript`; bus messages are not sent to viewers unless the
  board is public.
- **Stage streams**: the room metadata keeps the live screen shares and playback in `stage_streams` (oldest first).
  When the presented stream stops, the stage goes to the most recent stream still live, else to `presenter_prev`;
  Meet++ starting while a stream holds the stage follows it.
