# frontend

The user-facing single-page app. React 18 + Vite + TypeScript + Tailwind + Zustand. Lazy-loaded routes; emoji picker, PayPal SDK, LiveKit client, and most heavy deps are code-split per route.

## Quick links

- [Feature catalog](../docs/FEATURES.md) — page-by-page tour
- [docs/DEVELOPMENT.md → Frontend dev server](../docs/DEVELOPMENT.md#frontend-dev-server)

## Stack

| Concern | Library |
| --- | --- |
| Framework | [React 18](https://react.dev) + [Vite 5](https://vitejs.dev) |
| Language | TypeScript (strict mode) |
| Styling | [Tailwind CSS](https://tailwindcss.com) |
| State | [Zustand](https://github.com/pmndrs/zustand) |
| Routing | [react-router-dom v6](https://reactrouter.com) |
| LiveKit | [`livekit-client`](https://github.com/livekit/client-sdk-js) + [`@livekit/components-react`](https://github.com/livekit/components-js) |
| Virtual backgrounds | [`@livekit/track-processors`](https://github.com/livekit/track-processors-js) (MediaPipe wasm) |
| i18n | [i18next](https://www.i18next.com) + browser-language-detector |
| Markdown rendering | [react-markdown](https://github.com/remarkjs/react-markdown) + remark-gfm |
| Icons | [lucide-react](https://lucide.dev) |
| PDF (transcripts) | [jspdf](https://github.com/parallax/jsPDF) |
| QR codes (invites) | [qrcode](https://www.npmjs.com/package/qrcode) |
| Emoji picker | [emoji-picker-react](https://www.npmjs.com/package/emoji-picker-react) |
| PayPal Buttons | PayPal JS SDK (loaded from `www.paypal.com`) |

## Layout

```
src/
├── App.tsx                 # Top-level router (lazy-loaded routes)
├── main.tsx                # Entry point — mounts <App />
├── i18n.ts                 # i18next setup + server sync
├── styles/                 # Global CSS + Tailwind config
├── routes/                 # Pages (one component per URL)
│   ├── CreateMeeting.tsx   # Home — new meeting form + My Meetings
│   ├── Lobby.tsx           # Pre-meeting (password / name / waiting room)
│   ├── Room.tsx            # The full meeting surface
│   ├── PublicView.tsx      # /public/<slug> read-only viewer (embeddable)
│   ├── EgressLayoutPiP.tsx # Custom Web template for LiveKit Egress (PiP)
│   ├── EgressLayoutComposite.tsx # Alternative egress layout template
│   ├── Recordings.tsx      # List / download / publish to YouTube
│   ├── MeetingChat.tsx     # Post-meeting chat transcript (owner only)
│   ├── TICafe.tsx          # Always-on social audio room
│   ├── SignUp.tsx          # Native account signup
│   ├── Login.tsx           # Native login + 2FA branching
│   ├── ForgotPassword.tsx  # Request reset email
│   ├── ResetPassword.tsx   # Confirm reset with token
│   ├── SsoCallback.tsx     # postMessage handler for SSO bootstrap
│   ├── Account.tsx         # Profile + 2FA + subscription
│   ├── Settings.tsx        # Site-wide prefs
│   ├── Upgrade.tsx         # PayPal flow + voucher redeem
│   ├── Vouchers.tsx        # Voucher-admin issue / list / revoke
│   ├── AdminPanel.tsx      # Platform admin (users, IPs, IDS)
│   ├── Terms.tsx           # Static legal page
│   ├── Privacy.tsx         # Static legal page
│   └── Legal.tsx           # Static legal page
├── components/             # Reusable UI (see ../docs/FEATURES.md for descriptions)
└── lib/                    # Non-component domain code
    ├── api.ts              # Typed REST client
    ├── auth.ts             # Token storage + one.witysk.org bootstrap iframe
    ├── livekit.ts          # LiveKit SDK wrapper (room connect, track subscribe)
    ├── me.ts               # Authenticated-user store + profile
    ├── preferences.ts      # Server-synced UI prefs
    ├── privacy.ts          # Privacy-mode state (blur names/emails)
    ├── handRaise.ts        # Hand-raise state machine over data channel
    ├── joinPolicy.ts       # Lobby gate: password / name / waiting room
    ├── pushToTalk.ts       # Spacebar-to-talk handler
    ├── shortcuts.ts        # Keyboard shortcut registry + help overlay
    ├── sounds.ts           # Notification beeps
    ├── tiCafe.tsx          # TI Café context provider (persistent session)
    ├── themePref.ts        # Dark / light / system theme
    ├── uiPrefs.ts          # Sidebar / panel widths
    ├── videoQualityPref.ts # Bandwidth / resolution prefs
    ├── livestreamDestinations.ts # Per-platform stream-status polling
    ├── browserNotifications.ts   # Notification API wrapper
    ├── monoAudio.ts        # Mono audio processing (accessibility)
    └── animatedBackgrounds.ts    # Animated background for auth pages
```

## Building

```bash
npm install
npm run build       # tsc --noEmit && vite build → ./dist
```

The `dist/` folder is what Caddy serves at `/`. The build is reproduced in Docker by `frontend-build` (a one-shot container that copies `dist/` into a shared volume). For hot-reload dev, see [docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md).

## Bundle size

Lazy loading is the main lever. Without it, the LiveKit SDK + MediaPipe wasm + PayPal SDK + emoji picker hit the wire on every page. With it, only the routes the user visits get fetched. If you add a new route, follow the existing pattern in `App.tsx`:

```tsx
const NewRoute = lazy(() => import("./routes/NewRoute"));
```

## SSO bootstrap

The `bootstrapFromOneWitysk()` helper in `lib/auth.ts` mounts a hidden iframe to `https://one.witysk.org/sso-bootstrap.html`. The iframe reads the access_token from localStorage on the upstream origin (same-origin to itself) and postMessages it back. The SPA validates the message origin and stores the token.

To swap in a different SSO issuer:

1. Host an equivalent bootstrap page on the issuer origin (template in [`../one-witysk-integration/sso-bootstrap.html`](../one-witysk-integration/sso-bootstrap.html)).
2. Update the iframe URL + the `targetOrigin` check in `lib/auth.ts`.
3. Update the Caddy `frame-src` CSP directive in [`../caddy/Caddyfile`](../caddy/Caddyfile) to the new origin.

## Virtual backgrounds

[`@livekit/track-processors`](https://github.com/livekit/track-processors-js) loads MediaPipe wasm + tflite models from `cdn.jsdelivr.net` at runtime. Caddy's CSP allows `script-src` and `connect-src` for that origin. Without it, virtual backgrounds silently fail with a CSP violation in the console.
