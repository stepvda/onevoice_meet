# REST API reference

The `meeting-api` service exposes a versioned REST API under `/api/v1/*`. This document is a hand-edited summary of every endpoint; for live request/response shapes, hit `/api/docs` (FastAPI's Swagger UI) or `/api/openapi.json`.

## Conventions

- **Base URL** — `https://<your-host>/api`. The reference deployment is `https://meet.witysk.org/api`.
- **Authentication** — `Authorization: Bearer <jwt>` on every protected route. Two issuers, one secret, one algorithm (HS256); see [Architecture → Authentication](ARCHITECTURE.md#authentication--sessions).
- **Errors** — Standard FastAPI HTTP errors. `4xx` for bad input / auth, `5xx` for server-side problems. Rate-limited routes return `429` with a `Retry-After` header.
- **Idempotency** — Mutations are *not* idempotent unless noted. Don't retry blindly on 5xx without checking state first.

## Health

| Method | Path             | Auth | Purpose                              |
| ------ | ---------------- | ---- | ------------------------------------ |
| GET    | `/api/health`    | —    | Liveness probe (`{"status":"ok"}`).  |

## Meetings

| Method | Path                                       | Auth     | Purpose                                                                  |
| ------ | ------------------------------------------ | -------- | ------------------------------------------------------------------------ |
| POST   | `/api/v1/meetings`                         | Admin    | Create a meeting.                                                        |
| GET    | `/api/v1/meetings`                         | Any auth | List your meetings (with `?include_hidden`, `?include_past`).            |
| GET    | `/api/v1/meetings/discover`                | Any auth | List discoverable meetings (paginated).                                  |
| GET    | `/api/v1/meetings/discover/public`         | —        | List `list_for_anonymous=true` meetings (for the landing page).          |
| GET    | `/api/v1/meetings/{meeting_id}`            | Owner / co-host | Fetch full meeting details.                                       |
| PATCH  | `/api/v1/meetings/{meeting_id}`            | Owner    | Update meeting (title, schedule, policy, livestream URLs, public slug…). |
| DELETE | `/api/v1/meetings/{meeting_id}`            | Owner    | Soft-delete (hidden=true; recordings remain reachable).                  |
| POST   | `/api/v1/meetings/{meeting_id}/token`      | Owner    | Mint a LiveKit `room_admin` token for joining.                           |
| GET    | `/api/v1/meetings/{meeting_id}/participants` | Owner  | List all participants who have ever joined (incl. left).                 |
| GET    | `/api/v1/meetings/{meeting_id}/ics`        | —        | Download `.ics` (RFC 5545 iCalendar) for the meeting.                    |
| POST   | `/api/v1/meetings/{meeting_id}/branding`   | Owner    | Upload branding image (max 2 MB).                                        |
| DELETE | `/api/v1/meetings/{meeting_id}/branding`   | Owner    | Remove branding image.                                                   |
| GET    | `/api/v1/rooms/{room_name}/branding`       | —        | Public branding image (served in lobby).                                 |

## Anonymous join

| Method | Path                                       | Auth | Purpose                                                                            |
| ------ | ------------------------------------------ | ---- | ---------------------------------------------------------------------------------- |
| POST   | `/api/v1/rooms/{room_name}/anon-token`     | —    | Mint a LiveKit token for an anonymous joiner. Rate-limited at `ANON_TOKEN_RATE_PER_HOUR`. |
| GET    | `/api/v1/rooms/{room_name}/wait/{wait_token}` | —  | Poll waiting-room status (waiting / admitted / denied).                            |

## Moderation

| Method | Path                                        | Auth          | Purpose                                                |
| ------ | ------------------------------------------- | ------------- | ------------------------------------------------------ |
| POST   | `/api/v1/meetings/{meeting_id}/mute`        | Host / co-host | Mute one participant's audio.                          |
| POST   | `/api/v1/meetings/{meeting_id}/lower-hand`  | Host / co-host | Lower a raised hand.                                   |
| POST   | `/api/v1/meetings/{meeting_id}/update-presenter` | Host / co-host | Pin / unpin a presenter (writes `room.metadata`).      |
| POST   | `/api/v1/meetings/{meeting_id}/kick`        | Host / co-host | Remove participant; audited in `moderation_audit`.     |

## Waiting room

| Method | Path                                          | Auth          | Purpose                                       |
| ------ | --------------------------------------------- | ------------- | --------------------------------------------- |
| GET    | `/api/v1/meetings/{meeting_id}/pending`       | Host / co-host | List pending joiners.                         |
| POST   | `/api/v1/meetings/{meeting_id}/pending/{wait_token}:admit` | Host / co-host | Admit; returns the LiveKit token to the SPA. |
| POST   | `/api/v1/meetings/{meeting_id}/pending/{wait_token}:deny` | Host / co-host | Deny.                                          |

## Recordings

| Method | Path                                              | Auth          | Purpose                                                       |
| ------ | ------------------------------------------------- | ------------- | ------------------------------------------------------------- |
| POST   | `/api/v1/meetings/{meeting_id}/recordings:start`  | Host / co-host | Start RoomCompositeEgress. `layout` ∈ {speaker, grid, single-speaker, pip}. |
| POST   | `/api/v1/meetings/{meeting_id}/recordings:stop`   | Host / co-host | Stop recording.                                               |
| GET    | `/api/v1/meetings/{meeting_id}/recordings`        | Owner         | List recordings for a meeting.                                |
| GET    | `/api/v1/recordings`                              | Any auth      | List all recordings owned by the current user.                |
| GET    | `/api/v1/recordings/{recording_id}`               | Owner         | Recording metadata.                                            |
| DELETE | `/api/v1/recordings/{recording_id}`               | Owner         | Delete MP4 + transcript files; row stays for audit (status=deleted). |
| GET    | `/api/rec/{recording_id}/stream.mp4`              | Owner         | Stream / download the MP4 (range-supported).                  |
| GET    | `/api/v1/recordings/{recording_id}/transcript`    | Owner         | Plain-text transcript.                                         |
| POST   | `/api/v1/recordings/{recording_id}/publish-youtube` | Owner       | Resumable upload to YouTube.                                  |

## Livestreaming

| Method | Path                                          | Auth          | Purpose                                                              |
| ------ | --------------------------------------------- | ------------- | -------------------------------------------------------------------- |
| POST   | `/api/v1/meetings/{meeting_id}/stream:start`  | Host / co-host | Start livestream to configured destinations (X / Substack / YouTube / Facebook / Rumble). |
| POST   | `/api/v1/meetings/{meeting_id}/stream:stop`   | Host / co-host | Stop livestream.                                                     |
| GET    | `/api/v1/meetings/{meeting_id}/stream`        | Owner         | Status (enabled, active, current `egress_id`).                       |
| GET    | `/api/v1/meetings/{meeting_id}/stream/destinations` | Owner   | Per-platform publish status (`idle | streaming | failed | complete`) + last error string. |

## Video playback

| Method | Path                                                       | Auth          | Purpose                                                |
| ------ | ---------------------------------------------------------- | ------------- | ------------------------------------------------------ |
| GET    | `/api/v1/meetings/{meeting_id}/playback/items`             | Owner         | List playlist.                                         |
| POST   | `/api/v1/meetings/{meeting_id}/playback/items`             | Owner         | Upload MP4 (multipart/form-data).                      |
| POST   | `/api/v1/meetings/{meeting_id}/playback/items/{item_id}/alias` | Owner     | Create an alias of an existing item at a new position. |
| PATCH  | `/api/v1/meetings/{meeting_id}/playback/items/{item_id}`   | Owner         | Rename or reorder.                                     |
| DELETE | `/api/v1/meetings/{meeting_id}/playback/items/{item_id}`   | Owner         | Remove from playlist.                                  |
| POST   | `/api/v1/meetings/{meeting_id}/playback:start`             | Host / co-host | Start playing item 0.                                  |
| POST   | `/api/v1/meetings/{meeting_id}/playback:advance`           | Host / co-host | Skip to next item.                                     |
| POST   | `/api/v1/meetings/{meeting_id}/playback:pause`             | Host / co-host | Pause (freeze-frame stream).                           |
| POST   | `/api/v1/meetings/{meeting_id}/playback:resume`            | Host / co-host | Resume playback.                                       |
| POST   | `/api/v1/meetings/{meeting_id}/playback:stop`              | Host / co-host | Stop playback.                                         |
| GET    | `/api/v1/ingress/playback/{meeting_id}/{item_id}`          | HMAC-signed URL | Internal — streamed to LiveKit Ingress.              |

## Chat, reactions, notes, whiteboard

| Method | Path                                                       | Auth | Purpose                                            |
| ------ | ---------------------------------------------------------- | ---- | -------------------------------------------------- |
| GET    | `/api/v1/rooms/{room_name}/chat`                           | —    | Recent chat history (room-gated).                  |
| POST   | `/api/v1/rooms/{room_name}/chat`                           | —    | Post a text message.                               |
| POST   | `/api/v1/rooms/{room_name}/chat/attachment`                | —    | Post a message with an image attachment.           |
| GET    | `/api/v1/rooms/{room_name}/chat/{message_id}/attachment`   | —    | Fetch image attachment.                            |
| PUT    | `/api/v1/rooms/{room_name}/chat/{message_id}/reaction`     | —    | Add / replace emoji reaction.                      |
| DELETE | `/api/v1/rooms/{room_name}/chat/{message_id}/reaction`     | —    | Remove reaction.                                   |
| GET    | `/api/v1/meetings/{meeting_id}/chat`                       | Owner | Full transcript (post-meeting view).               |
| POST   | `/api/v1/meetings/{meeting_id}/chat/pin`                   | Owner | Pin / unpin a message.                             |
| GET    | `/api/v1/rooms/{room_name}/notes`                          | —    | Read collaborative notes.                          |
| PUT    | `/api/v1/rooms/{room_name}/notes`                          | —    | Update notes (max 100 KB).                         |
| GET    | `/api/v1/rooms/{room_name}/whiteboard/strokes`             | —    | All strokes + clear markers.                       |
| POST   | `/api/v1/rooms/{room_name}/whiteboard/strokes`             | —    | Append a stroke (5000-stroke cap).                 |
| DELETE | `/api/v1/rooms/{room_name}/whiteboard/strokes`             | —    | Clear all strokes.                                 |
| GET    | `/api/v1/rooms/{room_name}/whiteboard/shapes`              | —    | List persistent shapes.                            |
| PUT    | `/api/v1/rooms/{room_name}/whiteboard/shapes/{shape_id}`   | —    | Upsert a shape.                                    |
| DELETE | `/api/v1/rooms/{room_name}/whiteboard/shapes/{shape_id}`   | —    | Delete a shape.                                    |

Room-gated endpoints don't require a JWT but require knowing the `room_name` — assume any leaked room name lets an outsider read chat & notes. Use `require_password=true` if that matters.

## Polls & Q&A

| Method | Path                                                            | Auth          | Purpose                  |
| ------ | --------------------------------------------------------------- | ------------- | ------------------------ |
| GET    | `/api/v1/meetings/{meeting_id}/polls`                           | —             | List polls.              |
| POST   | `/api/v1/meetings/{meeting_id}/polls`                           | Host / co-host | Create poll (2–6 options). |
| POST   | `/api/v1/meetings/{meeting_id}/polls/{poll_id}:close`           | Host / co-host | Close poll.              |
| POST   | `/api/v1/meetings/{meeting_id}/polls/{poll_id}/vote`            | —             | Cast vote (one per voter). |
| GET    | `/api/v1/meetings/{meeting_id}/questions`                       | —             | List Q&A.                |
| POST   | `/api/v1/meetings/{meeting_id}/questions`                       | —             | Ask a question.          |
| PUT    | `/api/v1/meetings/{meeting_id}/questions/{question_id}:upvote`  | —             | Upvote.                  |
| POST   | `/api/v1/meetings/{meeting_id}/questions/{question_id}:answered` | Host / co-host | Mark answered.            |

## Auth (native accounts)

| Method | Path                                  | Auth | Purpose                                                              |
| ------ | ------------------------------------- | ---- | -------------------------------------------------------------------- |
| POST   | `/api/v1/auth/signup`                 | —    | Create native account, claim 10-day trial. Rate-limited.             |
| POST   | `/api/v1/auth/login`                  | —    | Login. Returns token, OR a `challenge_token` if 2FA is enabled.      |
| POST   | `/api/v1/auth/logout`                 | Any  | Stateless hook (client clears token).                                |
| POST   | `/api/v1/auth/login/2fa`              | —    | Complete TOTP login with the challenge token + code (or recovery).   |
| POST   | `/api/v1/me/email-otp/send`           | —    | Email a 6-digit OTP for login (5-min Redis TTL).                     |
| POST   | `/api/v1/auth/login/email-otp`        | —    | Complete login with the email OTP.                                   |
| POST   | `/api/v1/auth/password-reset/request` | —    | Mail a single-use reset link. Rate-limited; no user enumeration.     |
| POST   | `/api/v1/auth/password-reset/confirm` | —    | Set a new password using the token.                                  |

## Self-service profile

| Method | Path                                  | Auth | Purpose                                                              |
| ------ | ------------------------------------- | ---- | -------------------------------------------------------------------- |
| GET    | `/api/v1/me`                          | Any  | Fetch profile.                                                       |
| PATCH  | `/api/v1/me`                          | Any  | Update name, email, username (native only — SSO fields are read-only).|
| POST   | `/api/v1/me/start-trial`              | Any (native) | Claim the 10-day trial. One per account.                     |
| POST   | `/api/v1/me/password`                 | Any (native) | Change password.                                              |
| POST   | `/api/v1/me/facepic`                  | Any (native) | Upload avatar (5 MB cap; JPEG/PNG/WebP/GIF).                  |
| DELETE | `/api/v1/me/facepic`                  | Any (native) | Remove avatar.                                                |
| GET    | `/api/v1/users/{user_id}/facepic`     | —    | Public facepic file (if any).                                        |
| DELETE | `/api/v1/me`                          | Any (native) | Hard-delete account.                                          |
| GET    | `/api/v1/me/preferences`              | Any  | UI preferences.                                                      |
| PUT    | `/api/v1/me/preferences`              | Any  | Update UI preferences.                                               |

## 2FA

| Method | Path                          | Auth | Purpose                                                                  |
| ------ | ----------------------------- | ---- | ------------------------------------------------------------------------ |
| POST   | `/api/v1/me/2fa/setup`        | Any (native) | Generate TOTP secret + provisioning URI (not enabled yet).       |
| POST   | `/api/v1/me/2fa/enable`       | Any (native) | Verify code, enable, return recovery codes (one-time view).      |
| POST   | `/api/v1/me/2fa/disable`      | Any (native) | Disable (requires password + current TOTP or recovery code).     |
| POST   | `/api/v1/me/2fa/recovery`     | Any (native) | View remaining recovery codes (password-confirmed).              |

## Billing

| Method | Path                                       | Auth   | Purpose                                                                  |
| ------ | ------------------------------------------ | ------ | ------------------------------------------------------------------------ |
| GET    | `/api/v1/billing/config`                   | —      | Public PayPal config (client_id, plan_ids, prices, currency).            |
| POST   | `/api/v1/billing/orders`                   | Any    | Create one-shot PayPal order (`monthly` or `annual`).                    |
| POST   | `/api/v1/billing/orders/{order_id}/capture`| Any    | Capture an approved order; grants entitlement.                           |
| POST   | `/api/v1/billing/subscriptions/activated`  | Any    | Confirm subscription after PayPal JS SDK flow.                           |
| POST   | `/api/v1/billing/subscriptions/cancel`     | Any    | Cancel current monthly subscription.                                     |
| POST   | `/api/v1/billing/webhook`                  | —      | PayPal events (signature-verified using `paypal_webhook_id`).            |
| GET    | `/api/v1/billing/me/billing-history`       | Any    | Aggregate history (subscriptions, orders, voucher redemptions).          |

## Vouchers

| Method | Path                                | Auth                       | Purpose                                            |
| ------ | ----------------------------------- | -------------------------- | -------------------------------------------------- |
| POST   | `/api/v1/vouchers`                  | Voucher admin              | Issue a voucher (8-char code, configurable duration). |
| GET    | `/api/v1/vouchers`                  | Voucher admin              | List vouchers + redemption status.                 |
| DELETE | `/api/v1/vouchers/{voucher_id}`     | Voucher admin              | Revoke an unredeemed voucher.                      |
| POST   | `/api/v1/vouchers/{code}/redeem`    | Any (native)               | Redeem and gain entitlement.                       |

## TI Café

| Method | Path                          | Auth | Purpose                                                |
| ------ | ----------------------------- | ---- | ------------------------------------------------------ |
| POST   | `/api/v1/ti-cafe/join`        | SSO  | Mint a LiveKit token for the always-on Café room.      |
| GET    | `/api/v1/ti-cafe/live`        | Any  | Live participant count + list.                         |
| DELETE | `/api/v1/ti-cafe/leave`       | SSO  | Leave hook (optional; LiveKit detects disconnect).     |

## Platform admin

| Method | Path                                 | Auth           | Purpose                                                  |
| ------ | ------------------------------------ | -------------- | -------------------------------------------------------- |
| GET    | `/api/v1/admin/users`                | Platform admin | List/search/filter users.                                |
| GET    | `/api/v1/admin/users/{user_id}`      | Platform admin | Fetch a user.                                            |
| PATCH  | `/api/v1/admin/users/{user_id}`      | Platform admin | Toggle admin/disable, update fields.                     |
| POST   | `/api/v1/admin/users/{user_id}/set-password` | Platform admin | Force-reset password (native only).               |
| DELETE | `/api/v1/admin/users/{user_id}`      | Platform admin | Hard-delete a user (not self).                           |
| GET    | `/api/v1/admin/blocked-ips`          | Platform admin | List blocklist.                                          |
| POST   | `/api/v1/admin/blocked-ips`          | Platform admin | Add entry (IP / CIDR / dash range).                      |
| PATCH  | `/api/v1/admin/blocked-ips/{ip_id}`  | Platform admin | Toggle enabled or update reason.                         |
| DELETE | `/api/v1/admin/blocked-ips/{ip_id}`  | Platform admin | Remove entry.                                            |
| GET    | `/api/v1/admin/ids/status`           | Platform admin | IDS stats (tracked IPs, temp blocks).                    |
| GET    | `/api/v1/admin/ids/events`           | Platform admin | Paginated security event log.                            |
| POST   | `/api/v1/admin/ids/unblock/{ip}`     | Platform admin | Lift a temp block.                                       |

## LiveKit webhook receiver

| Method | Path                              | Auth                         | Purpose                                       |
| ------ | --------------------------------- | ---------------------------- | --------------------------------------------- |
| POST   | `/api/v1/webhooks/livekit`        | LiveKit-signed JWT in header | Receive `room_finished`, `participant_*`, `egress_*`, `ingress_*` events. |

The webhook handler verifies the `Authorization: <jwt>` header against `LIVEKIT_WEBHOOK_KEY` before processing. Loopback-only — Caddy doesn't expose this externally; LiveKit POSTs to `http://127.0.0.1:8080/api/v1/webhooks/livekit`.
