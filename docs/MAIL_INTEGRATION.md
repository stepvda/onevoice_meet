# One Voice Mail integration

One Voice Mail (mail.witysk.org) uses Meet in three ways:

1. **Embedded Meet** — Mail's `/meet` page frames the real SPA
   (`https://meet.witysk.org/<path>?embed=ovm&parent=<Mail origin>`). The SPA
   runs in embed mode: no One Voice SSO, the access token comes from Mail by
   postMessage, Mail's colour scheme.
2. **Online meetings** — Mail creates, updates and ends Meet meetings for its
   calendar events and its Meetings app, as the Mail user who turned the online
   meeting on.
3. **Meet++ for Mail meetings** — Mail pushes the agenda, decisions, actions and
   roster of a sitting to Meet++, and reads back the minutes, decisions,
   actions and attendance when the AI meeting ends.

The shared contract (wire shapes, postMessage protocol, Mail's side) is Mail's
`docs/meet-integration.md`; this file describes Meet's side. Mail's client and
its in-process fake Meet (`apps/services/src/meet/`,
`apps/services/test/helpers/fake-meet.ts`) are the reference for the shapes.

## Configuration

| Variable | Production value | Meaning |
| --- | --- | --- |
| `MAIL_ISSUER` | `https://mail.witysk.org` | Mail's JWT issuer. Empty = integration off. |
| `MAIL_JWKS_URL` | _(default)_ | Default `<MAIL_ISSUER>/.well-known/jwks.json`. |
| `MAIL_AUDIENCE` | _(default `meet`)_ | Required `aud`. |
| `MAIL_WEBHOOK_URL` | `https://mail.witysk.org/api/meet/webhook` | Webhook target; empty = no webhooks. |
| `MAIL_WEBHOOK_SECRET` | secret | Same value as Mail's `MEET_WEBHOOK_SECRET`. |
| `EMBED_PARENT_ORIGINS` | `https://mail.witysk.org` | Origins the SPA accepts as embedding parent (comma-separated). |
| `MEET_FRAME_ANCESTORS` | `https://mail.witysk.org` | Caddy only: `frame-ancestors` of the SPA pages (space-separated). |

## Auth (`app/auth.py`)

Mail mints an ES256 JWT per user: header `{alg: ES256, kid, typ: JWT}`, claims
`iss` (Mail's issuer), `aud` (`meet`), `sub` (Mail user id), `type: access`,
`email`, `name`, `onevoice_sub` (only when the user linked One Voice), `iat`,
`exp` (15 min), `jti`.

- A token whose unverified `iss` equals `MAIL_ISSUER` is verified against Mail's
  JWKS only (own cache, 10 minutes; an unknown `kid` refetches once, forced
  refetches at most every 10 s), pinned to ES256, `aud` and `exp` required.
  Every other token is verified as before (one.witysk.org HS256/ES256, Meet
  native).
- Principal: with `onevoice_sub` → the SSO user with that `external_id`
  (auto-provisioned), `AuthUser.sub = onevoice_sub` — the same Meet account as
  at meet.witysk.org. Otherwise a `kind="mail"` user with
  `external_id = "mail:<sub>"`, `AuthUser.sub = "mail:<sub>"` (LiveKit identity
  `user-mail:<sub>`, Meet++ person key `sub:mail:<sub>`).
- Email and name follow the claims; an email another account already uses is
  not taken (the sync is skipped). A `kind="mail"` user never becomes a
  platform admin through `PLATFORM_ADMIN_EMAILS`.
- Mail users always have meeting-creation rights and use Meet++ like SSO
  users. They have no trial, billing or vouchers, cannot edit their profile or
  facepic in Meet, and `POST /v1/auth/refresh` refuses Mail tokens (the
  embedded SPA asks Mail for a fresh token).

## Meetings (`app/routes/meetings.py`, `app/routes/tokens.py`)

- `POST /api/v1/meetings` also takes `allow_anonymous` and
  `integration: {source: "mail", ref}` (stored as `external_source`,
  `external_ref`; Mail uses `event:<uid>` and `meeting:<id>`).
- `PATCH /api/v1/meetings/{id}` also takes `scheduled_at`, `ends_at`,
  `duration_minutes`, `recurrence_rule` (Meet's presets; anything else
  clears it), `allow_anonymous` and `password` (`null` or `""` removes it and
  turns `require_password` off). A field left out is unchanged; an explicit
  `null` clears a time. Owner or co-host, as before.
- `duration_minutes` outside 5–480 is clamped (create and PATCH) instead of
  refused, and `display_name` is cut to 120 characters: Mail sends any
  calendar event's length and a user's name or e-mail.
- `MeetingOut` adds `join_url`, `allow_anonymous`, `has_password`,
  `external_source`, `external_ref`.
- `POST /api/v1/rooms/{room}/anon-token` admits guests when
  `list_for_anonymous` **or** `allow_anonymous` (the password check is
  unchanged). `allow_anonymous` never lists the meeting anywhere.
- `GET /api/v1/embed/config` (no auth) → `{parent_origins: [...]}`.

## Meet++ for a Mail meeting (`app/meetpp/external.py`)

`PUT /api/v1/meetings/{mid}/meetpp/external` (chair) prepares one sitting.
Body: Mail's `MeetppSyncPayload` (`occurrence_ref`, `series_ref`, `title`,
`starts_at`, `duration_minutes`, `meeting_type`, `open_url`, `agenda[]`,
`decisions[]`, `actions[]`, `roster[]`, `next_occurrence`). Response
`{session_id, status, series_id}`.

- Series: `meetpp_series.external_ref = "mail:<series_ref>"` (of the meeting's
  owner), created when missing; `meetings.meetpp_series_id` points at it.
- Session: the meeting's session with `external_ref = occurrence_ref`, created
  (template agenda, mode lead, status setup) when missing. Setup sessions of
  other sittings are deleted. Another sitting running, paused or finalising →
  409 `meetpp_busy`; this sitting finalising, review, published or aborted →
  409 `meetpp_closed`.
- **setup**: the agenda points are replaced from `agenda` (sections keep the
  Mail ref in `external_ref`; a point the chair edited in Meet — `locked` —
  keeps its text; sub-points added in Meet stay). Decisions are upserted by ref
  under their point, `pending` unless Mail says adopted/rejected/withdrawn;
  Mail decisions no longer listed are removed unless edited in Meet. Actions
  are upserted by ref as previous actions of the series (origin `mail`,
  "to review"); done/cancelled ones Mail sends are not imported; Mail's status
  is not applied while a sitting that reported on the action awaits review in
  Meet. Open Mail actions Mail no longer lists leave the sitting: deleted when
  they only exist for it, else hidden there (`external_json.left_action_ids`).
  The roster is upserted by e-mail (voting as Mail says); expected attendees
  follow. A Mail user joining is matched to their roster row by e-mail or name.
- **running / paused**: additive — new points, decisions, actions and roster
  members are added; titles and bodies of items nobody edited in Meet follow
  Mail (a decision only while still pending); nothing is removed.
- Sections created from Mail have `source: "mail"`; decisions and actions
  `origin: "mail"`.

`GET /api/v1/meetpp/sessions/{sid}/external` (chair) → the outcome in Mail's
terms: `session {id, status, occurrence_ref, started_at, ended_at,
published_at}`, `minutes_markdown` (what `export.md` returns, `""` before
review), `decisions` (pending never-taken ones left out; `ref` = Mail id or
null, `agenda_ref`, `vote {for, against, abstain, result}`), `actions` (all
actions of the sitting; AI `proposed` reported as `open`; assignee e-mails from
Mail, the roster, attendees or accounts), `attendance` (`not_registered` →
`absent`), `next_agenda`, `next_agenda_sent`.

`POST /api/v1/meetpp/sessions/{sid}/external/next-agenda` (chair) body
`{agenda: [{title, body?}]}` → `{ok, external, next_agenda}`: stores the
agenda (also as the review's next agenda), sets `next_agenda_sent` and sends
the `meetpp.next_agenda` webhook. 409 for a session not prepared from Mail.

`SessionMeta.external` (state snapshot, deltas and `GET …/review`, which also
has it at the top level) = `{source, occurrence_ref, open_url,
next_occurrence: {ref, starts_at} | null, next_agenda_sent}` or `null`.
`GET /meetings/{mid}/meetpp/sessions` items carry `external_ref` for prepared
sessions. The review draft of a prepared session proposes the next sitting's
date and does not send Meet's own invitations by default (Mail sends them).

## Webhook (`app/services/mail_webhook.py`)

For meetings with `external_source = "mail"`: a Meet++ session reaching
`running` (start or resume), `review`, `published` or `aborted` →
`meetpp.status`; the draft agenda sent → `meetpp.next_agenda`. Body
`{event, meeting_id, session_id, occurrence_ref, status, at}`; headers
`X-Meet-Timestamp: <unix seconds>`,
`X-Meet-Signature: v1=<hex HMAC-SHA256(secret, timestamp + "." + raw body)>`.
Status changes are caught by one ORM listener on `MeetppSession.status` and
sent after the commit. Delivery is a background thread: attempts now, +30 s,
+5 min (re-signed each time), failures logged (`mail webhook … not
delivered`); it never blocks or fails the request. Mail also polls.

## Security headers (`caddy/Caddyfile`)

The SPA pages (`spa_headers`: the catch-all and `/on-demand`) send
`frame-ancestors 'self' {$MEET_FRAME_ANCESTORS}` and no `X-Frame-Options`.
`/public/*`, `/egress-layout/*` and `/embed/on-demand` keep their own
policies; `Permissions-Policy` stays self-scoped (Mail's iframe delegates
camera, microphone and the rest with its `allow` attribute).

## Testing

```bash
cd meeting-api
pytest tests/test_mail_auth.py tests/test_mail_meetings.py tests/meetpp/test_external.py
```

The tests mint ES256 tokens with a generated P-256 key (fixture `mail` in
`tests/conftest.py` patches the JWKS fetch) and record webhook deliveries
instead of sending them.

Against a dev Mail: run meeting-api with `MAIL_ISSUER` set to the dev Mail's
issuer (e.g. `http://localhost:5180`), `MAIL_JWKS_URL` pointing at the dev
services (`http://127.0.0.1:3250/.well-known/jwks.json`),
`MAIL_WEBHOOK_URL=http://127.0.0.1:3250/api/meet/webhook`, the same
`MAIL_WEBHOOK_SECRET` as Mail's `MEET_WEBHOOK_SECRET`, `MEETPP_ENABLED=true`
and `EMBED_PARENT_ORIGINS` = the dev Mail origin, and point Mail's `MEET_URL`
at the SPA dev server. Without LiveKit, everything but joining a room works.
