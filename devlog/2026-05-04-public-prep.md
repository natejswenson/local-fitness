# 2026-05-04 — public-prep

First devlog entry. Today the repo went from a personal scratchpad to
something that can live in public on GitHub as `local-fitness-dude`.

## What changed

- **Path defaults are project-relative.** `db.py`, `notes.py`,
  `briefing.py`, and `web/server.py` previously hardcoded
  `~/localrepo/local-fitness/data` (and `briefings/`) as fallback when
  the `LOCAL_FITNESS_*` env vars were unset. Now they resolve against
  the project root via `Path(__file__).resolve().parents[N]`. Same
  physical paths in this checkout, but a fresh clone elsewhere
  (`~/code/local-fitness/`, `/opt/...`) just works.
- **`.env` instead of in-source defaults.** Added `python-dotenv` and a
  one-liner loader at the top of `cli.py`. `.env.example` is committed
  with documented placeholders for the four `LOCAL_FITNESS_*` vars and
  the two `GARMIN_*` vars; `.env` is gitignored. Real shell-set vars
  still take precedence (Docker compose path is unaffected).
- **Launchd plist is a template now.** The old
  `ops/com.local-fitness.daily.plist` had `/Users/natejswenson/...`
  hardcoded four times. Replaced with `.plist.template` containing
  `__PROJECT_ROOT__` / `__HOME__` placeholders; `install-launchd.sh`
  renders to `*.plist.rendered` (gitignored) and copies to
  `~/Library/LaunchAgents/`.
- **Sanitised user-facing strings.** Dropped the hardcoded "Nate"
  default in `fitness setup`'s name prompt. README path examples no
  longer reference a specific home directory.
- **Wider gitignore.** Added `.env`, `.env.local`, `ops/*.plist.rendered`,
  and `.claude/` (Claude Code runtime cache).

## What didn't change

- **Garmin auth flow.** Was already env-aware
  (`GARMIN_EMAIL` / `GARMIN_PASSWORD` win over Keychain). Host CLI
  still defaults to Keychain via `fitness setup`. No secrets in code,
  before or after.
- **`pyproject.toml` author block.** Standard package metadata —
  matches what GitHub commit history will show anyway.
- **Git history.** Old commits still contain the hardcoded paths and
  email. Force-rewriting was rejected: cost (broken clones,
  contributor confusion) outweighs the benefit (hiding info already
  visible on the GitHub profile).

## Verification

- `uv run fitness --help` — clean import, dotenv loader runs.
- `uv run fitness status` — DB resolves to the same 73 MB
  `data/fitness.db` it always has. 2,072 daily metrics, 667 activities.
- `pytest -x` — 4 passed.
- `sed`-rendered plist points at the correct absolute paths.

## Going forward

Use `/devlog` to auto-generate future entries from git commits. This
file is the manual prefix.
