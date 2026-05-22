# Contributing

Thanks for considering a contribution! `onevoice-meet` is a small project run by one maintainer, so PR triage may be slow — please bear with it.

## Before you start

1. **Open an issue first** for non-trivial work. A 15-minute conversation up front saves a 3-day rewrite later.
2. **Check the reference deployment.** [meet.witysk.org](https://meet.witysk.org) is what the maintainer runs; behavior there is the source of truth for "how it works." If you're proposing a behavior change, screenshot the current behavior in your issue.
3. **Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — it'll save you reverse-engineering time.

## How to contribute

- **Bugs** — Open an issue with reproduction steps. Include the version of the relevant container (`docker compose images`), your browser, and any console / server log lines that look relevant.
- **Documentation** — PRs to the `docs/` folder and inline comments are always welcome. The bar is "would a stranger arriving cold understand this in 10 minutes?"
- **Features** — Open an issue first, especially for anything that touches the LiveKit integration, the egress / livestream layer, or the auth system. These have non-obvious constraints (CPU costs, single-egress-worker, dual-issuer JWT) that are easy to miss.
- **Tests** — The biggest open ask is the Playwright suite (mentioned in the [README status section](README.md#status)). Real end-to-end browser tests covering: create-meeting flow, anonymous join, password gate, waiting room, recording start/stop, livestream destinations, playback.

## Development setup

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Style

- **Python** — 4-space indent. Imports sorted in three sections (stdlib / third-party / app). Type hints encouraged but not required (the existing codebase is partially typed). No Black configured — match the surrounding code.
- **TypeScript** — Strict mode on. Functional components only. Zustand for cross-component state. New deps require justification (the bundle is already large).
- **Comments** — Explain *why*, not what. Don't document obvious code. Don't add `// TODO`s without an issue link.
- **Commits** — Conventional commits not required, but a clear one-line subject plus a body explaining the why is appreciated.

## Pull request checklist

- [ ] One PR per concern. Don't bundle unrelated changes.
- [ ] If the change affects the API surface, update [docs/API.md](docs/API.md).
- [ ] If the change adds a new `.env` knob, update [docs/CONFIGURATION.md](docs/CONFIGURATION.md) **and** `.env.example`.
- [ ] Backend changes: `pytest` passes from `meeting-api/`.
- [ ] Frontend changes: `npm run build` passes from `frontend/`.
- [ ] Manual test the affected path against a real LiveKit instance (docker compose up). Code that compiles isn't the same as code that works.

## Code of conduct

Be kind. Disagree on the technical merits, never on the person. Reports of unacceptable behavior to **stephane@stepvda.com**.
