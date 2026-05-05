# 2026-05-05 — security fixes from yesterday's audit

Closing out the four findings flagged in the 2026-05-04 adversarial scan.

## Path traversal in the SPA fallback (HIGH — confirmed exploit)

`server.py:703-710` was happily serving anything `WEB_DIST / full_path`
resolved to, with no containment check. A `curl --path-as-is
http://host/../../pyproject.toml` against the running server returned the
file. Anyone on the LAN with a non-normalizing client could read
`data/fitness.db`, `.env`, `briefings/*.json`, or any file the FastAPI
process could open.

Fix: `(WEB_DIST / full_path).resolve()` followed by `.relative_to(...)`.
Escape attempts return the SPA shell instead. Live-verified with the
same `curl --path-as-is` payloads — now 200 with the 885-byte
`index.html`, not the file contents.

Regression test: `tests/test_security.py::test_spa_fallback_blocks_path_traversal`.
Hits the ASGI scope directly with raw `..` segments (httpx normalizes
client-side, which is why the bug looked safe in casual testing).

## No auth on /api/* (HIGH)

The server's own docstring acknowledged the gap. It's now enforced.

- New env var: `LOCAL_FITNESS_API_TOKEN`. When set, every `/api/*`
  request must carry `Authorization: Bearer <token>`. Constant-time
  compare via `secrets.compare_digest`.
- `/health` stays public for Traefik's healthcheck.
- Startup safety: `serve()` refuses to bind to a non-loopback host
  (anything but `127.0.0.1` / `localhost` / `::1`) without the token
  set. Hard exit, loud log line. Loopback dev path still works
  token-less for host CLI convenience.
- Frontend: new `AuthGate` component wraps the route tree. On first
  paint it probes `/api/auth/verify`; on 401 it shows a single-input
  login screen. Token persists in `localStorage` per device — the
  inbound `Authorization` header is added by a `withAuth()` wrapper in
  `api.ts`. On any later 401 (e.g., server token rotated), the gate
  re-prompts mid-session without a page reload.

## Rate limit on Claude-cost endpoints (MEDIUM)

In-memory token bucket on `/api/chat` and `/api/brief/generate*`. 20
requests per IP per minute, returning 429 + `Retry-After` when
exhausted. Loopback IPs are exempt (the local UI shouldn't throttle
itself). The middleware sits OUTSIDE auth, so a flood with bad tokens
still gets capped — prevents the obvious "burn the wallet by guessing
tokens" pattern.

## Defense-in-depth headers (LOW)

Added a 4-line middleware that sets `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and a tight
`Permissions-Policy`. Cheap, no functional cost.

## Dependabot (LOW)

`.github/dependabot.yml` enrolls pip / npm / docker / github-actions on
a weekly Monday cadence. CVEs land as PRs instead of waiting for the
next manual audit.

## Verification

- `uv run pytest -x` — 10 / 10 passing (4 smoke + 6 new security).
- `pnpm build` — clean, no TS errors.
- Live probes:
  - `curl --path-as-is /../../pyproject.toml` → 885-byte SPA shell, not
    pyproject contents.
  - With `LOCAL_FITNESS_API_TOKEN=…`: no-token request → 401, bad-token
    → 401, good-token → 200, `/health` → 200.
  - `fitness serve --host 0.0.0.0` without the token → exit 1 + clear
    error log.
  - `curl -I /health` shows all four security headers.

## Skipped, documented

- **`run_sql` denylist hardening.** Held up against every bypass tried
  (comment prefixes, semicolon chains, case mixing — `sqlite3.execute`
  also single-stmt-only). With `/api/chat` now token-gated, the
  prompt-injection-then-SQL exfil path requires auth anyway. The
  denylist is fine for v1.
- **`pyproject.toml` author email.** Standard package metadata, kept by
  prior decision.
- **`curl | sh`** for `uv` install in `Dockerfile`. Accepted supply-chain
  dependency on `astral.sh`. Not changing.

## Devlog cadence

This is the second manual entry. Next batch via `/devlog`.
