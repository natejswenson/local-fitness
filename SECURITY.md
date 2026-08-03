# Security policy

## Reporting a vulnerability

**Please report privately, not as a public issue.**

Use GitHub's [private vulnerability reporting](https://github.com/natejswenson/local-fitness/security/advisories/new)
— the "Report a vulnerability" button under the repo's **Security** tab. That
opens a private advisory only you and the maintainer can see.

This is a personal project maintained by one person in their spare time, so
please calibrate your expectations accordingly:

| | |
|---|---|
| First response | Usually within a week |
| Fix for a confirmed issue | Best effort, prioritised by real-world impact |
| Coordinated disclosure | Happy to, on whatever timeline you need |
| Bounty | None — this is unpaid hobby software |

There is no embargo requirement. If you would rather just open a public issue
because the finding is low severity, that is fine too.

## What versions are supported

The **latest release only.** There are no maintenance branches and no
backports — `main` is the public snapshot, `dev` is where work lands. If you
are running an older tag, the fix will be "upgrade".

## What this software actually is

Worth stating plainly, because it shapes what counts as a vulnerability.

`local-fitness` is a **single-user personal application**. It pulls one
person's Garmin Connect data into a local SQLite database and exposes it to an
LLM coach over MCP. It is not multi-tenant, has no user accounts, no roles, and
no notion of one user's data being hidden from another — there is only ever one
user, and that user is the person running it.

The intended deployment is a laptop or a container on a home LAN behind a
reverse proxy. It is **not** designed to be exposed to the public internet, and
doing so is outside the threat model rather than a supported configuration.

## In scope

Things that are genuinely bugs, and that I want to hear about:

- **Authentication bypass** on the HTTP surface — anything that reaches `/mcp/`
  without a valid bearer token when `LOCAL_FITNESS_API_TOKEN` is set. (This has
  happened: 0.43.1 fixed a `Host`-header path-poisoning bypass.)
- **SQL injection**, or any way to get a write through `run_sql`, which is
  supposed to be read-only by construction (`mode=ro` URI, statement-prefix
  gate, keyword denylist, 5s deadline, 500-row cap).
- **Path traversal** — reading or writing a file outside the intended directory
  via any user-supplied path segment.
- **Secret disclosure** — a Garmin credential, `CLAUDE_CODE_OAUTH_TOKEN`,
  `ANTHROPIC_API_KEY`, or `LOCAL_FITNESS_API_TOKEN` reaching a log, an error
  payload, a generated PDF, or the git history.
- **Remote code execution** by any route.
- **Prompt injection that reaches a write tool** — text from an external source
  (a Garmin activity name, a synced description) steering the agent into
  calling `update_plan_workout`, `abandon_active_plan`, `save_user_note`,
  `update_coach_personality`, or `sync_garmin_data`. Note the four LLM call
  sites are deliberately toolless (`max_turns=1`, no MCP servers), so a working
  chain here would be a real finding.
- **Denial of service that survives a restart** — something that corrupts the
  database or wedges the server permanently.

## Out of scope

Not because they don't matter, but because they are known, accepted, or
inherent to what this is:

- **Running it exposed to the internet.** Don't. `serve()` refuses to start on
  a non-loopback bind without `LOCAL_FITNESS_API_TOKEN` precisely because that
  configuration is a mistake.
- **The single authenticated user doing anything at all.** There is one user
  and they own the data. "An authenticated caller can read all the health data"
  is the product working.
- **`run_sql` reading tables beyond the advertised schema.** It is bounded to
  read-only, but it is not table-scoped. Everything in that database is already
  reachable through the structured tools by design.
- **Transient rate-limit exhaustion against Garmin.** Calling
  `sync_garmin_data` in a tight loop will trip Garmin's own limiter and require
  an interactive re-auth. Annoying, self-inflicted, recoverable.
- **Anything requiring physical or shell access to the host.** If you are
  already on the machine, the SQLite file and `.env` are right there.
- **Missing hardening on a threat this design doesn't carry** — no CSRF (no
  cookies, no browser surface, no forms), no XSS (the web UI was retired in
  2026-07; the server hosts an MCP transport and `/health`, and serves no HTML).
- **Dependency advisories with no reachable code path.** Report them if you
  like — Dependabot already does — but a CVE in a transitive package the app
  never calls is a bump, not an incident.

## What is already in place

So you can skip re-finding these:

- Bearer-token gate on every path except `/health`; `_is_public_path` is
  **deny-by-default** and reads `request.scope["path"]`, never a Host-derived
  URL.
- `serve()` refuses to bind non-loopback without a token.
- Every SQL string built with a column or table name whitelisted against a
  frozen set; all values parameterised.
- `run_sql` opens a genuinely read-only connection — SQLite refuses the write,
  rather than the code merely avoiding one.
- Path joins with user-supplied segments go through a
  `.resolve().relative_to()` containment check.
- The two PDF-writing tools are structurally unreachable over the network
  transport, not merely undocumented there.
- Secrets live only in the environment and the OS keychain — never in the
  database, never in a tracked file. `data/`, `briefings/`, `logs/` and `.env`
  are gitignored.
- Secret scanning, push protection and Dependabot security updates are enabled
  on the repository.
- `tests/test_security.py` is the regression net; every auth-relevant fix adds
  a case there.

## If you are deploying this yourself

Three things that matter more than anything in the code:

1. **Set `LOCAL_FITNESS_API_TOKEN`** to a long random string if the server is
   reachable by anything other than loopback.
2. **Set `TZ`** in the container. Unset means UTC, and this app is
   date-anchored everywhere — an evening request answers confidently about the
   wrong day.
3. **Keep it off the public internet.** A reverse proxy on a home network is
   the intended deployment.
