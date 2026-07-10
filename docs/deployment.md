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
      # container's session. The host CLI now also resolves+writes this same
      # file by default (daily._tokenstore_path → ~/.garminconnect/
      # garmin_tokens.json), so host and container share one token and the
      # seeding flow actually works; GARMINTOKENS here is the explicit
      # container override of that default.
      - GARMINTOKENS=/home/app/.garminconnect/garmin_tokens.json
      # Long-lived Claude Code subscription token (so the Agent SDK
      # subprocess can authenticate without per-request API billing)
      - CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN}
      # Bearer token gating /mcp/ (and every other non-public path) —
      # REQUIRED when binding 0.0.0.0
      - LOCAL_FITNESS_API_TOKEN=${LOCAL_FITNESS_API_TOKEN}
      # MCP server host allowlist — MUST include the served host or every
      # /mcp/ request 421s (DNS-rebinding guard). Default includes
      # fitness.home.local; set explicitly if you serve at a different host.
      - LOCAL_FITNESS_MCP_ALLOWED_HOSTS=${LOCAL_FITNESS_MCP_ALLOWED_HOSTS:-fitness.home.local,127.0.0.1,localhost}
      # Display units for runner-facing output (mi, min/mi). Raw meters/sec-per-km
      # are always present; non-"miles" only suppresses the *_mi fields. Default miles.
      - LOCAL_FITNESS_DISPLAY_UNITS=${LOCAL_FITNESS_DISPLAY_UNITS:-miles}
    volumes:
      - ./data:/data
      - ./briefings:/briefings
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

There is no browser session — every client is an MCP client (Claude
Code/Desktop/Mobile, opencode) authenticating directly via the
`Authorization: Bearer <token>` header on the `/mcp/` transport. Configure
the token once in each client's MCP server config; a request with no or
the wrong header gets a 401 from the bearer middleware.

### Rotating the token

1. Generate a new token (same `secrets.token_urlsafe(32)` snippet).
2. Update the compose-side `.env`.
3. `docker compose up -d local-fitness` (no rebuild needed; env-only
   change recreates the container).
4. Every previously-configured client's next `/mcp/` request now 401s —
   update the token in that client's MCP server config (wherever the
   `Authorization: Bearer` header is set) and it resumes.

## Host CLI / dev mode

`uv run fitness serve` defaults to `127.0.0.1:8765` and accepts no
token by default — the loopback-bind exemption keeps host-CLI dev
ergonomic. Set `LOCAL_FITNESS_API_TOKEN` in this repo's `.env` when
you want auth even on loopback (rare).

## Scheduled brief job (the composer is a separate process)

The web server runs **no Claude inference** — the daily brief is written
out-of-band by `fitness brief`, which composes it via the Claude-bound
headless agent (in-process MCP, no HTTP transport) and persists it through
`briefs.save_brief()`. Schedule that command however your host runs cron-style
jobs:

- **macOS (host CLI):** `./ops/install-launchd.sh` installs a launchd agent
  that runs `fitness brief` daily at 06:30 (catch-up at next wake if the Mac
  was asleep). See `ops/README.md`.
- **Linux / container host:** add a cron entry or systemd timer for
  `uv run fitness brief` in the repo directory.

Either way the job needs **`CLAUDE_CODE_OAUTH_TOKEN`** (Claude Max
subscription — no per-token billing) in the `.env` the CLI loads. It needs
neither `LOCAL_FITNESS_API_TOKEN` nor an MCP allowed-host, because the
composer reaches the tools in-process rather than over the `/mcp/` endpoint.
For the container, `CLAUDE_CODE_OAUTH_TOKEN` is already wired into the compose
`environment:` block above; for the host CLI it goes in this repo's `.env`.
