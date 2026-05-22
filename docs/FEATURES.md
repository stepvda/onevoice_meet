# Features

A page-by-page tour of the SPA, with the backend endpoints each page relies on. The reference deployment ([meet.witysk.org](https://meet.witysk.org)) is the easiest way to see these in action.

## 1. Home — `/`

Route: [`frontend/src/routes/CreateMeeting.tsx`](../frontend/src/routes/CreateMeeting.tsx)

The starting point. For authenticated users:

- **Create a new meeting.** Pick a display title, optional schedule (`scheduled_at` + `duration_minutes`), optional iCalendar recurrence rule (`FREQ=WEEKLY;COUNT=10`), and a moderation policy:
  - Auto-admit authenticated users
  - Require name on join
  - Auto-mute new joiners
  - Auto-disable camera for new joiners
  - Waiting-room flow
  - Lock the room after the first join
  - Allow / forbid participant screenshare
  - Allow / forbid participant chat
- **My Meetings list.** Past, upcoming, and hidden meetings the user owns. Soft-delete (hidden) so recordings stay reachable.
- **Discoverable meetings.** Any meeting with `list_for_authenticated=true` shows up here for any signed-in user. With `list_for_anonymous=true`, anonymous visitors see it too on the public landing page.

Backend: `POST /api/v1/meetings`, `GET /api/v1/meetings`, `PATCH /api/v1/meetings/{id}`.

## 2. Lobby — `/<room-name>` (also `/j/<room-name>`)

Route: [`frontend/src/routes/Lobby.tsx`](../frontend/src/routes/Lobby.tsx)

Pre-meeting page. Mic / camera preview, virtual background picker, device selector. Joins enforce the meeting's moderation policy:

- Password gate (`require_password=true` on the meeting).
- Name-required gate. If `require_name_on_join=true`, the join button is disabled until a name is entered.
- Waiting room. If enabled, the join button switches to "Knock on the door" and the page polls `/rooms/{room_name}/wait/{wait_token}` until the host clicks Admit.

Backend: `POST /api/v1/rooms/{room_name}/anon-token`, `GET /api/v1/rooms/{room_name}/wait/{wait_token}`.

## 3. The meeting itself — `/r/<room-name>`

Route: [`frontend/src/routes/Room.tsx`](../frontend/src/routes/Room.tsx)

The full call surface. Components composed here:

- **Video grid** with auto-spotlight on the active speaker, or pinned presenter via the moderator menu. ([PresenterSpotlight.tsx](../frontend/src/components/PresenterSpotlight.tsx))
- **Picture-in-picture button** ([PipButton.tsx](../frontend/src/components/PipButton.tsx)) — toggle the custom PiP recording / livestream layout.
- **Reactions** — floating emoji over the grid that fade after a few seconds. ([FloatingReactions.tsx](../frontend/src/components/FloatingReactions.tsx), [ReactionsButton.tsx](../frontend/src/components/ReactionsButton.tsx))
- **Hand raise** ([HandRaiseButton.tsx](../frontend/src/components/HandRaiseButton.tsx)) — sends a data-channel signal; the moderator menu shows raised hands.
- **Push-to-talk** ([PushToTalkIndicator.tsx](../frontend/src/components/PushToTalkIndicator.tsx)) — hold space to unmute (configurable in Settings).
- **Output volume control** ([OutputVolumeControl.tsx](../frontend/src/components/OutputVolumeControl.tsx)) — per-tab speaker volume slider.
- **Captions overlay** ([CaptionsOverlay.tsx](../frontend/src/components/CaptionsOverlay.tsx)) — live speech-to-text overlay (browser-side Web Speech API; opt-in).
- **Audio waveforms** ([AudioWaveform.tsx](../frontend/src/components/AudioWaveform.tsx)) — animated bars around the active speaker tile.
- **Recording indicator** ([RecordingIndicator.tsx](../frontend/src/components/RecordingIndicator.tsx)) — red dot + elapsed time when recording.
- **Streaming indicator** ([StreamingIndicator.tsx](../frontend/src/components/StreamingIndicator.tsx)) — broadcast badge when livestreaming.
- **Meeting clock** ([MeetingClock.tsx](../frontend/src/components/MeetingClock.tsx)) — current time + remaining duration.
- **Invite modal** ([InviteModal.tsx](../frontend/src/components/InviteModal.tsx)) — copy join link, copy room code, QR code (uses [qrcode](https://www.npmjs.com/package/qrcode)).

### Side panels

The right rail toggles between:

- **Chat** ([ChatPanel.tsx](../frontend/src/components/ChatPanel.tsx)) — text + emoji picker + replies + image attachments (5 MB cap). Reactions per message. Owner can pin / unpin messages. Live fan-out is a small "refetch" signal over the LiveKit data channel; the persisted truth lives in `chat_messages`.
- **Participants** ([ParticipantsPanel.tsx](../frontend/src/components/ParticipantsPanel.tsx)) — list with online/offline, hand-raised indicator, moderator actions.
- **Polls + Q&A** ([PollsQnaPanel.tsx](../frontend/src/components/PollsQnaPanel.tsx)) — host creates polls (2–6 options) and closes them; anyone can ask a question; anyone can upvote; host marks answered.
- **Notes + Whiteboard** ([NotesWhiteboardPanel.tsx](../frontend/src/components/NotesWhiteboardPanel.tsx)) — collaborative plain-text notes (last-writer-wins, debounced); free-draw strokes (append-only, replayable for late joiners); persistent shapes (rect / ellipse / text — editable).
- **Video playback** ([VideoPlaybackPanel.tsx](../frontend/src/components/VideoPlaybackPanel.tsx)) — host-only. Upload MP4s, reorder, play, pause, resume, loop, "What's up next" toggle.
- **Pending joiners** ([PendingJoinersPanel.tsx](../frontend/src/components/PendingJoinersPanel.tsx)) — when the waiting room is enabled.

### Moderator menu — `ModeratorMenu.tsx`

Host- and co-host-only. Per participant:

- Mute / unmute audio
- Pin / unpin as presenter
- Lower hand
- Kick from room (audit-logged in `moderation_audit`)
- Promote to co-host (writes to `cohost_user_ids` JSON on the meeting; participant must rejoin to pick up the moderator token)

Per meeting:

- Start / stop recording (layout picker: speaker, grid, single-speaker, PiP)
- Start / stop livestream (with destination toggles)
- Toggle PiP egress layout
- Open the in-meeting settings modal ([InMeetingSettings.tsx](../frontend/src/components/InMeetingSettings.tsx))

Backend: `/api/v1/meetings/{id}/mute|kick|lower-hand|update-presenter`, `/api/v1/meetings/{id}/recordings:start|stop`, `/api/v1/meetings/{id}/stream:start|stop`, `/api/v1/meetings/{id}/playback:*`.

## 4. Public view — `/public/<slug>`

Route: [`frontend/src/routes/PublicView.tsx`](../frontend/src/routes/PublicView.tsx)

A read-only viewer for any meeting the owner has marked `public_enabled=true` with a custom `public_slug`. Visitors:

- Don't sign in.
- Don't show up in the participants panel.
- Can't publish audio or video.
- Don't count toward the 50-participant limit.

Caddy serves this path with `X-Frame-Options` omitted and `frame-ancestors *` in CSP, so it's safely embeddable in any iframe — e.g. on a blog post or marketing page.

## 5. Recordings — `/recordings`

Route: [`frontend/src/routes/Recordings.tsx`](../frontend/src/routes/Recordings.tsx)

Per-user list of recordings owned by the current account. For each row:

- File size, duration, recorded-at.
- Download MP4.
- Delete (frees disk).
- View transcript (if Whisper produced one).
- Publish to YouTube — one click. Pops a modal with title, description, privacy (public / unlisted / private), then triggers a resumable upload via `services/youtube.py`.

Backend: `GET /api/v1/meetings/{id}/recordings`, `GET /api/rec/{id}/stream.mp4`, `DELETE /api/v1/recordings/{id}`, `POST /api/v1/recordings/{id}/publish-youtube`, `GET /api/v1/recordings/{id}/transcript`.

## 6. Meeting chat (post-meeting) — `/meetings/<meeting-id>/chat`

Route: [`frontend/src/routes/MeetingChat.tsx`](../frontend/src/routes/MeetingChat.tsx)

Owner-only transcript view. Reads the same `chat_messages` table the in-meeting chat panel writes to, but unauthenticated participants can't get here.

## 7. TI Café — `/ti-cafe`

Route: [`frontend/src/routes/TICafe.tsx`](../frontend/src/routes/TICafe.tsx)

Always-on social audio room. SSO-only (anonymous users get redirected). Mic-only — no video, no chat. The bar at the top of every page ([TICafeBar.tsx](../frontend/src/components/TICafeBar.tsx)) shows the live-participant count.

Backend: `POST /api/v1/ti-cafe/join`, `GET /api/v1/ti-cafe/live`, `DELETE /api/v1/ti-cafe/leave`.

## 8. Account — `/account`

Route: [`frontend/src/routes/Account.tsx`](../frontend/src/routes/Account.tsx)

Profile management for native users (most fields hidden for SSO users since their profile lives on the upstream issuer):

- Display name, email, username (`PATCH /api/v1/me`).
- Avatar upload (`POST /api/v1/me/facepic`, max 5 MB, JPEG/PNG/WebP/GIF).
- Change password (`POST /api/v1/me/password` — confirms current password first).
- **Two-factor authentication** ([TwoFactorSettings.tsx](../frontend/src/components/TwoFactorSettings.tsx)):
  - TOTP setup — scan QR code, confirm with a code, receive single-use recovery codes.
  - Email-OTP enable — 6-digit code mailed at login, 5-minute TTL.
- **Subscription status** ([SubscriptionStatus.tsx](../frontend/src/components/SubscriptionStatus.tsx)):
  - Trial days remaining.
  - Active subscription plan (monthly / annual / voucher).
  - Cancel button for active monthly subscription.
  - Billing history (subscriptions, one-shot orders, voucher redemptions).
- **Delete account** — hard delete, password-confirmed.

## 9. Upgrade — `/upgrade`

Route: [`frontend/src/routes/Upgrade.tsx`](../frontend/src/routes/Upgrade.tsx)

PayPal flow. Three options:

- **Monthly subscription** — recurring billing via PayPal `Subscriptions` API. `paypal_plan_id_monthly` in `.env`. Cancellable from Account.
- **Bill-once monthly** — one-shot Order, 30-day entitlement, no auto-renewal.
- **Bill-once annual** — one-shot Order, 365-day entitlement.
- **Redeem a voucher** — paste the 8-character code (skips 0/O/1/I/L for clarity), get a 30-day entitlement.

Webhooks from PayPal hit `/api/v1/billing/webhook` and are signature-verified using the `paypal_webhook_id` from the PayPal dashboard.

## 10. Vouchers — `/vouchers`

Route: [`frontend/src/routes/Vouchers.tsx`](../frontend/src/routes/Vouchers.tsx)

For users whose `user_id` is in `VOUCHER_ADMIN_USER_IDS` in `.env`. Issue codes, set duration (in days), add a note ("paid Jane €20 cash"), revoke unredeemed codes, see the redemption status of every issued voucher.

## 11. Admin panel — `/admin`

Route: [`frontend/src/routes/AdminPanel.tsx`](../frontend/src/routes/AdminPanel.tsx)

For platform admins (`is_platform_admin=true`). Three tabs:

- **Users.** Search, paginate, filter by kind / disabled. For each user: toggle admin, disable / enable, force-reset password (native only), delete (hard).
- **Blocked IPs.** Add / remove blocklist entries — exact IP, CIDR (`203.0.113.0/24`), or dash range (`203.0.113.5-50`). Shows hit counters incremented every time the middleware rejects a request.
- **IDS status.** In-memory event log (auth fails, path scans, 2FA fails), currently temp-blocked IPs, total tracked IPs, manual "unblock" button.

## 12. Auth pages

- `/signup` ([SignUp.tsx](../frontend/src/routes/SignUp.tsx)) — native signup, password-strength meter, 10-day trial auto-claimed.
- `/login` ([Login.tsx](../frontend/src/routes/Login.tsx)) — login form, branches to TOTP or email-OTP if 2FA is on.
- `/forgot-password` ([ForgotPassword.tsx](../frontend/src/routes/ForgotPassword.tsx)) — sends a reset email via Resend.
- `/reset-password` ([ResetPassword.tsx](../frontend/src/routes/ResetPassword.tsx)) — confirm reset with token + new password.
- `/sso-callback` ([SsoCallback.tsx](../frontend/src/routes/SsoCallback.tsx)) — receives the postMessage from the `one.witysk.org` SSO bootstrap iframe.

## 13. Settings — `/settings`

Route: [`frontend/src/routes/Settings.tsx`](../frontend/src/routes/Settings.tsx)

Site-wide preferences:

- Language (i18next; `PUT /api/v1/me/preferences`).
- Theme (dark / light / system).
- Default audio / video devices.
- Privacy mode (blur real names + emails in participant lists & chat).
- Push-to-talk on/off + key.
- Video quality preference (bandwidth limit).
- Browser notification permission helper.
- Anonymise email in join log + suppress IP logging — server-enforced via `UserPreferences`.

## 14. Legal — `/terms`, `/privacy`, `/legal`

Static pages, swap with your own deployment's content.

---

## Cross-cutting features

### Internationalisation

UI strings live in [i18next](https://www.i18next.com/) bundles. The current preferred-language is stored server-side per user (`UserPreferences.language`) and resyncs on every page load — so changing language on one device propagates everywhere. Two helper scripts in [`scripts/`](../scripts/) find and fill in missing translations.

### Email delivery

Transactional email goes via [Resend](https://resend.com/). Set `RESEND_API_KEY` and `FROM_EMAIL` in `.env`. Used for:

- Account welcome
- Password reset
- Email-OTP login codes
- Transcript delivery once Whisper finishes a recording

Templates are in [`meeting-api/app/services/email_templates.py`](../meeting-api/app/services/email_templates.py) — dark blue / green palette, inline CSS for cross-client compatibility, f-string substitution.

### YouTube manual publish

One-time consent flow via [`scripts/youtube_oauth.py`](../scripts/youtube_oauth.py): opens a browser, you click Allow, the script writes the refresh token to `.env`. After that, every "Publish to YouTube" click on the Recordings page does a resumable upload with the configured `youtube_default_privacy`.

### iCalendar export

Every meeting can export to `.ics` for calendar apps. Recurrence rules (`recurrence_rule` on the meeting row) are passed through verbatim — RFC 5545 syntax is supported as-is.

### Branding

The owner can upload a per-meeting branding image (max 2 MB) — shown in the lobby and the top bar of the meeting page. Served from `/api/v1/rooms/{room_name}/branding`. Useful for events.

### "What's up next" rundown slide

A broadcast-style 35-second slide inserted before any playlist item over 5 minutes when the toggle is on. Shows the meeting title, the next 5 items, and a countdown. Rendered with Pillow + numpy; cached by content hash; pre-generated when the toggle flips so the first auto-advance is instant.

### Intrusion detection + IP blocking

[`intrusion_detector.py`](../meeting-api/app/services/intrusion_detector.py) tracks auth failures, 2FA failures, and 404 scans per-IP in sliding windows; auto-temp-blocks (default 30 min) when thresholds are crossed. Persistent blocks live in `blocked_ips` and are managed from the admin panel. The [`IPBlockMiddleware`](../meeting-api/app/services/ip_block.py) sits outermost in the FastAPI middleware stack so blocked addresses never hit auth or DB.
