# Meet++ FDD v2 — adversarial review and disposition

Reviewer: engineering (implementation team)
Date: 3 October 2026
Scope: `docs/MeetPlusPlus_FDD_v2.docx` (all sections and all 11 figures)
Baseline reviewed: main @ working tree with the Meet++ implementation deployed.

This addendum records the adversarial review of the FDD, the improvements it
produced, and where the shipped Release 1 deliberately deviates from the
document. It supersedes the corresponding statements in the docx until the
docx itself is revised.

## Method

Every functional claim in §5–§9 and §7 was checked against the implementation;
each figure was compared with the shipped UI; every numeric assumption
(§11.1 host size, §13 latency/token model, §16 gates) was checked against the
production host and observed behaviour.

## Findings and dispositions

### D1 — Tick trigger policy was unimplementable as written (fixed)

The FDD §8.2 requires a tick when any of: ≥120 new words, ≥45 s since the
last tick, a cue phrase, or a phase/item change. In practice the *first* tick
never fired: `last_tick_at` is NULL at start, so "≥45 s since the last tick"
evaluated to 0 for ever, and short utterances never reached 120 words. Live
extraction therefore only ran after several minutes.

**Disposition:** the trigger now treats "no previous tick" as due, and ticks
at least every few seconds (loop wake 4 s, idle threshold 3 s) with the cue
phrase short-circuiting immediately. The FDD text is superseded: Release 1
processes transcript windows close to per-utterance, not per-120-words. The
token budget was raised (900k → 2M input/hour, §13.2 / `MEETPP_MAX_TOKENS_PER_HOUR`)
to pay for the higher tick rate.

### D2 — Versioning could silently disable follow mode (fixed)

FDD §7.5 promises the board switches to the tab that changed. The server
incremented `state_version` on every tick even when no operation mutated
state, so the delta the client received carried a version gap
(`local + 2`, `local + 3`, …). The client correctly fell back to a full
`GET /state?since=` refresh — which does not carry the focus hint — and the
board stopped auto-switching.

**Disposition:** `state_version` now increments only when an operation
actually changes state; presence/consent updates increment it too so the
Attendance tab updates live with an inline delta.

### D3 — Participants already in the room never mounted the board (fixed)

FDD §7.1 assumes every participant mounts the board on `session started`.
The runtime never sent that message: only the chair (which fetches state
after `POST /start`) saw the board, badge and consent dialog.

**Disposition:** the runner broadcasts `{type:"session", state:"started"}` on
start; late joiners discover the session through
`GET /meetpp/rooms/{room}/active` and the lobby notice (§7.7, now implemented
in `Lobby.tsx`).

### D4 — Consent was opt-out, not opt-in (fixed)

FDD §12.3 and NFR-7 state that consent is collected **before** transcribing,
and opted-out audio never reaches STT. The implementation subscribed to every
microphone and dropped only explicit `opt_out` identities, i.e. transcription
started before anyone answered the dialog.

**Disposition:** consent is default-deny. The agent subscribes only to
identities with a stored `accept` consent, and the server additionally drops
any segment from an identity without an `accept` row. The consent dialog can
no longer be dismissed without a choice.

### D5 — "Not yet" did not stop auto-advance (fixed)

FDD §8.5: a rejected proposal suppresses the same target for 3 minutes. The
suppression record kept the original `auto_at`, so in Lead mode the rejected
transition auto-accepted 8 s later.

**Disposition:** `phases.suppress` clears `auto_at`; the runtime ignores
`auto_at` while a suppression window is active.

### D6 — Capacity cap was off by one (fixed)

FDD NFR-2 / FR-S6 cap concurrent sessions at `MEETPP_MAX_ACTIVE_SESSIONS`.
`_active_session_count` excluded `setup` and the start check used `>`, so two
setup sessions could both start with the cap at 1.

**Disposition:** the start check compares the *other* live sessions with `>=`.

### D7 — Uploaded PDFs were never sent (fixed)

FDD Figure 3 / FR-S2 require the agenda and previous-notes PDFs to be uploaded
and parsed. The setup modal collected the files but the layer's `onStart`
handler forwarded only the first argument, so no `POST /documents` ever ran
(the production database had zero `meetpp_documents` rows).

**Disposition:** the files are forwarded and uploaded; the agenda populates
the Agenda tab (`import_agenda`), the previous notes populate series actions
and decisions, and both PDFs appear in the Attachments tab.

### D8 — Structured agenda/notes call sites were broken (fixed)

`ingest.structure_document` called the prompt builders positionally against
keyword-only signatures and used a non-existent function name, so every PDF
raised `TypeError`/`AttributeError` and was marked failed.

**Disposition:** calls corrected to `text=`/`language=` and
`build_notes_messages`; regression tests added.

### D9 — HTML output was not escaped (fixed)

FDD §12.4 (stored XSS) requires AI and transcript text to be escaped. The
Jinja environment used `select_autoescape(["html", "xml"])`, which matches on
filename suffix — and the templates are named `*.html.j2`, so escaping was
off. WeasyPrint then resolved any injected `file://`/`http://` resource
during publish (blind SSRF / local file read).

**Disposition:** `autoescape=True`; a regression test asserts a script tag in
an attendee name is escaped in the minutes HTML.

### D10 — Read-only room tokens could write (fixed)

FDD §10.4 marks the egress page and public viewers read-only. The attachment
upload route fell through to "any participant" for tokens that
`principal_can_edit` rejected, including `viewer-` and recorder tokens, which
also injected synthetic transcript rows.

**Disposition:** `principal.read_only` is rejected before the allowance check.

### D11 — PDF rendering was broken in the image (fixed)

`weasyprint==62.3` is incompatible with `pydyf>=0.11`
(`'super' object has no attribute 'transform'`), so every "PDF" was the
HTML fallback bytes and the minutes PDF never rendered.

**Disposition:** pinned `pydyf==0.10.0`; verified `%PDF-` output for both
minutes and agenda.

### D12 — Minutes PDF did not follow the house report format (fixed)

The generated minutes did not match the reference meeting report (header
identity, metadata block, attendance tables, numbered agenda, decisions,
follow-up actions, page footer).

**Disposition:** `templates/minutes.html.j2` rewritten to the reference
structure (org identity from `MEETPP_ORG_*`, page footer with
`counter(page)/counter(pages)`, attendance summary and member table,
numbered Agenda/Decisions/Follow-up actions). Finalisation now *adds*
decisions it finds (it previously only updated ones with an ID), and can
store a general minutes block when there are no agenda items.

### D13 — Piper TTS is not installable on the R1 image (documented deviation)

FDD §9.10 and ADR-9 select Piper for announcements. `piper-tts` depends on
`piper-phonemize`, which has no Python 3.12 Linux wheel; the image therefore
ships without Piper and `/tts` returns 503 when no voice is available.

**Disposition:** accepted R1 deviation. Clients fall back to
`window.speechSynthesis` for the spoken part (the text overlay is always
shown), and the egress page plays `audio_url` when present. The agent keeps
the `/tts` contract so a Mac Studio or a future wheel can be enabled by
configuration. The FDD should state the fallback.

### D14 — Figure 2 board had no interactive surface (fixed)

The FDD board mockup shows inline confirm/edit/reject, add links, phase jump,
counters, timer, provenance, NEW badges and the update footer. The first
implementation rendered the board read-only and never mounted the editable
copy, so no human could confirm or reject anything.

**Disposition:** the board is rendered editable in the chair panel
(right-hand drawer) with the mockup's visual language (white card, blue
accents, status pills, amber highlight, inline ✓/✎/✕, add links, timer,
`AI updated Xs ago · state vN`). The stage tile stays read-only for
participants, and a camera strip remains visible below it.

### D15 — Setup modal (Figure 3) missed its inputs and styling (fixed)

**Disposition:** the modal now matches Figure 3: structure radio cards,
language/mode/series row, two dashed PDF drop zones with status chips, amber
consent notice, and a green **Start AI Meeting** action. "Editors…" and the
Lead/Assist mode toggle were added to the session menu (FDD §5.2).

### D16 — Review screen (Figure 5) was a flat page (fixed)

**Disposition:** rebuilt as the three-column review screen: dark title bar
with a Draft/Published pill, section navigation with counts and change
dots, minutes editor with per-item headings and whiteboard snapshots, and
Next-meeting + Distribution cards with toggles and **Publish & send**.

### D17 — Editor role existed only in the authorisation matrix (fixed)

FDD §10.4 lists an Editor who may confirm, edit and add. The validator
supported editors but nothing could designate one.

**Disposition:** the AI menu has **Editors…**, listing attendees and their
LiveKit identities, saved through `PATCH /meetpp/sessions/{sid}`; the
validator and room-token auth already honour the list.

### D18 — Board operations promised in §7.4 were missing (fixed)

**Disposition:** added `agenda.update` (title/presenter/timebox/reorder) and
UI for add/edit agenda items, add decision, add action, attendance apologies
and name/e-mail edit, attachment rename/delete, per-item minutes regenerate,
and transcript search.

### D19 — Observability and operator controls promised in §14 were missing (fixed)

**Disposition:** structured `MEETPP_SESSION`, `MEETPP_TICK`,
`MEETPP_PROPOSAL` and `MEETPP_OUTPUT` log lines with the documented fields;
an Admin-panel **Meet++** tab showing enabled state, breaker, tokens today,
agent health, active sessions (with an operator **End** kill switch) and the
last rejected operations.

### D20 — AI evaluation harness promised in §16.3 was missing (implemented)

**Disposition:** `meeting-api/tools/meetpp_eval.py` replays a session's stored
segments through the live tick prompt and the configured provider, reporting
JSON validity, operation counts and token usage, with an option to dump the
raw ops for rubric scoring.

### D21 — Lobby notice promised in §7.7 was missing (fixed)

**Disposition:** the lobby shows a purple "AI notes on" notice when
`GET /meetpp/rooms/{room}/active` reports an active session.

### D22 — Accessibility gaps vs §7.10 (fixed)

**Disposition:** `Ctrl+Shift+A` is registered, listed in ShortcutOverlay, and
the board exposes a polite `aria-live` region throttled to one summary per
10 s; captions keep `aria-live="off"`.

### D23 — Stale statements in the docx to correct in a future revision

- §2.2 "2 vCPU" comments: production is 4 vCPU / 7.7 GB RAM (already noted in
  §11.2, but §2.2 still says 2 vCPU in places).
- §8.2 trigger policy (see D1).
- §9.10 Piper (see D13).
- §10.5 `LLM_FALLBACK` was documented but never implemented; the setting was
  removed from the configuration surface.
- §13.2 token budget: 900k is now 2M input tokens/hour by default.
- §16.3 harness path is now `meeting-api/tools/meetpp_eval.py`.
- The `.env` list should include `MEETPP_ORG_*` (report identity) and note
  that adding unrelated keys to `.env` no longer recreates LiveKit (the
  LiveKit service now receives only its three `LIVEKIT_*` variables).

### D24 — Operational follow-ups recorded, not code issues

- The batch whisper transcript e-mail (Q11) still sends to captured
  participants without a consent step and its `.txt` files are not deleted;
  Meet++ skips the batch job when it covered the recording, but the legacy
  path should still be gated.
- `livekit-server` is still `:latest`; pin it after the M0/TITV validation
  window as §11.4 requires.
- The finalisation "review ready" e-mail/banner is not sent; the review route
  is opened from the end-of-meeting modal instead.
- Public view honours `show_public`, but the setting has no UI yet (default
  off, so the board is not leaked).
