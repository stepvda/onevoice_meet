# Caddy snippet for one.witysk.org

meet.witysk.org loads `https://one.witysk.org/sso-bootstrap.html` inside a
hidden iframe to read the access_token from one.witysk.org's localStorage.
This is blocked by onevoice's default security headers:

- `X-Frame-Options: SAMEORIGIN`
- `Content-Security-Policy: … frame-ancestors 'self' …`

Add a scoped override so **only** `/sso-bootstrap.html` is embeddable by
meet.witysk.org. Drop the `@sso_bootstrap` matcher and its `handle` block into
onevoice's Caddyfile, ABOVE the existing site-wide header block.

## Patch

```caddyfile
one.witysk.org {
    # ... existing tls / encode / etc lines ...

    # ── NEW: scoped exception for the SSO bootstrap page ──────────────
    @sso_bootstrap path /sso-bootstrap.html
    handle @sso_bootstrap {
        # Remove the site-wide DENY/SAMEORIGIN header for this path only.
        header -X-Frame-Options

        # Replace the site-wide CSP with a narrower one that permits framing
        # by meet.witysk.org (and self). Keep it minimal — this file needs
        # almost nothing beyond same-origin script execution.
        header Content-Security-Policy "default-src 'none'; script-src 'self' 'unsafe-inline'; frame-ancestors 'self' https://meet.witysk.org; base-uri 'none'; form-action 'none'"

        # The file lives in the static frontend dir; adjust root if needed.
        root * /Users/nstephane/Dev/onevoice/react/frontend/dist
        file_server
    }
    # ──────────────────────────────────────────────────────────────────

    # ... existing reverse_proxy / root / file_server blocks ...
    # ... existing site-wide header { X-Frame-Options SAMEORIGIN; ... } block
    #     (unchanged; still applies to all other paths)
}
```

> ⚠️ Caddy processes `handle` blocks in the order they appear, and a matched
> `handle` stops further processing for that request. The `@sso_bootstrap`
> block must come BEFORE the SPA-fallback block (the one with `try_files`
> or `root`+`file_server`), otherwise the SPA fallback keeps winning and
> the site-wide headers still apply.

## Apply

```bash
# Edit the Caddyfile
vi /path/to/onevoice/Caddyfile    # or however you deploy

# Validate & reload — no downtime, no cert re-issuance
caddy validate --config /path/to/onevoice/Caddyfile
caddy reload  --config /path/to/onevoice/Caddyfile
```

If Caddy runs in Docker, the equivalent is `docker exec <caddy-container> caddy reload`.

## Verify

```bash
# The response for the bootstrap page should have NO X-Frame-Options
# and the CSP should list meet.witysk.org in frame-ancestors.
curl -sSI https://one.witysk.org/sso-bootstrap.html | grep -iE 'x-frame|frame-ancestors'
# expected output:
#   content-security-policy: ...; frame-ancestors 'self' https://meet.witysk.org; ...
# (no x-frame-options line at all)

# The rest of the site is unchanged — SPA still denies framing:
curl -sSI https://one.witysk.org/ | grep -iE 'x-frame|frame-ancestors'
# expected:
#   x-frame-options: SAMEORIGIN
#   content-security-policy: ...frame-ancestors 'self'...
```

After the reload, the meet.witysk.org SSO bootstrap flow works end-to-end.

## Why not just relax the global headers?

`X-Frame-Options: SAMEORIGIN` and `frame-ancestors 'self'` protect the
onevoice SPA from clickjacking — crucial for an auth-bearing application.
Scoping the exception to a single, ~30-line static HTML file (which does
nothing but read localStorage and postMessage) preserves protection
everywhere else.
