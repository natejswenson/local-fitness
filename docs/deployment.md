# Deployment

This repo ships only the application — the runtime container topology
(reverse proxy, host DNS, bind mounts) lives in a separate
infrastructure repo that isn't checked in here. This doc captures
what the *deploying* side has to wire up so a fresh checkout works
end-to-end.

## Container deployment (Traefik or any reverse proxy)

The `Dockerfile` produces an image that:

- Binds `0.0.0.0:8765` (so Docker port-forwarding can reach it).
- Reads `LOCAL_FITNESS_DATA_DIR=/data` and
  `LOCAL_FITNESS_BRIEFINGS_DIR=/briefings` from `ENV`, expecting bind
  mounts into the host's `data/` and `briefings/` directories so the
  host CLI and the container share state.
- **Refuses to start on a non-loopback host without
  `LOCAL_FITNESS_API_TOKEN`** (added 2026-05-05 after the security audit).

### Required env vars on the compose side

Inject these into the `local-fitness` service block. The token is
required; the others depend on whether you want the container to
do live Garmin pulls (host-CLI seeding works too).

```yaml
services:
  local-fitness:
    build:
      context: ./../local-fitness
    environment:
      # Garmin Connect (when the container does its own pulls — the
      # host CLI's macOS Keychain isn't reachable from a Linux container)
      - GARMIN_EMAIL=${LOCAL_FITNESS_GARMIN_EMAIL}
      - GARMIN_PASSWORD=${LOCAL_FITNESS_GARMIN_PASSWORD}
      # garminconnect token cache — point at the bind-mounted host
      # ~/.garminconnect dir so the host's first-MFA login seeds the
      # container's session
      - GARMINTOKENS=/home/app/.garminconnect/garmin_tokens.json
      # Long-lived Claude Code subscription token (so the Agent SDK
      # subprocess can authenticate without per-request API billing)
      - CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN}
      # Bearer token gating /api/* — REQUIRED when binding 0.0.0.0
      - LOCAL_FITNESS_API_TOKEN=${LOCAL_FITNESS_API_TOKEN}
    volumes:
      - ${HOME}/localrepo/local-fitness/data:/data
      - ${HOME}/localrepo/local-fitness/briefings:/briefings
      - ${HOME}/.garminconnect:/home/app/.garminconnect
      # Container's own writable .claude (the host's macOS keychain
      # auth isn't bind-mountable — run `docker exec -it fitness claude`
      # once for the OAuth flow, persists to a named volume)
      - fitness-claude-config:/home/app/.claude
```

The compose-side `.env` file (sibling of `docker-compose.yml`, same
shape as this repo's `.env.example`) supplies the four uppercase
variables. Generate the API token once with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Append to the compose `.env`, then bring the container up:

```bash
docker compose up -d --build local-fitness
```

### Per-device login

The web UI checks `/api/auth/verify` on first paint. With auth on, the
unauthenticated probe returns 401 and the `AuthGate` component shows
a single-input login form. Paste the value of
`LOCAL_FITNESS_API_TOKEN` once per device — it persists in the
browser's `localStorage` and the user never sees the screen again
unless the server token rotates.

### Rotating the token

1. Generate a new token (same `secrets.token_urlsafe(32)` snippet).
2. Update the compose-side `.env`.
3. `docker compose up -d local-fitness` (no rebuild needed; env-only
   change recreates the container).
4. Every previously-logged-in device's next request returns 401, the
   `AuthGate` re-prompts mid-session, user pastes new token, done.

## Host CLI / dev mode

`uv run fitness serve` defaults to `127.0.0.1:8765` and accepts no
token by default — the loopback-bind exemption keeps host-CLI dev
ergonomic. Set `LOCAL_FITNESS_API_TOKEN` in this repo's `.env` when
you want auth even on loopback (rare).
