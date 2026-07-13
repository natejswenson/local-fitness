---
ticket: "N/A"
title: "MCP tool call efficiency + visual UX polish"
date: "2026-07-10"
source: "design"
---

# MCP tool call efficiency + visual UX polish

## Goal

Three small, independent fixes to the existing `agent/tools.py` MCP surface
(~35 `@tool` functions) that any MCP client (Claude Desktop, Code, Mobile,
opencode) calls against — no new tools, no transport/architecture change.
This is a polish pass, scoped down from a broader "efficiency + visual UX"
ask after investigation showed most of that ask was already solved by
existing architecture (see *Ruled out* below).

## Ruled out by investigation

- **Backend DB/connection efficiency** is a separate, already-designed axis
  — see `2026-07-09-mcp-speed-and-ui-retirement-design.md` Part A. Not
  revisited here.
- **Client-agnostic formatting** was the original 3rd dimension considered
  for this design. Surprise: it's already solved. `mcp_server.py`'s
  `_install_coach_persona()` sets the MCP server's `instructions` field to
  the full coach system prompt (tables, one-word headers, lead-with-answer,
  metric translation, texting tone) on **every** client connection, not
  gated behind invoking the `coach` prompt. `_augment_workout()` already
  bakes miles/pace/duration into every workout-touching tool's raw data.
  The one real gap found in this area is Fix C below, which is a data-layer
  bug, not a missing formatting-guidance layer.
- **New aggregate tools for round-trip efficiency.** The aggregate already
  exists (`daily_snapshot` / `status.assemble_status()`). The actual
  problem was a *duplicate*, weaker tool sitting next to it — Fix B.
- **Other analysis tools** (`compare_periods`, `correlate`,
  `recovery_pattern`, `find_anomalies`, `training_load_status`) were
  checked and are fine as raw numeric payloads — they feed the agent's own
  synthesis by design, not meant to be pre-formatted.

## Fix A — Inline chart images

`generate_chart` (`agent/tools.py:1981`) already produces PNG bytes in
memory via `visuals.render_chart_png()` (returns plain `bytes` —
`buf.getvalue()`, `visuals.py:146`, not an `io.BytesIO` — so there's no
buffer-rewinding step needed, only a straight `base64.b64encode()`) before
writing them to a file and calling macOS `open()`. It's `LOCAL_ONLY_TOOLS`
specifically because a networked/phone MCP client has no way to retrieve a
local file path.

**Change:** add a second content block to `generate_chart`'s response —
`{"type": "image", "data": base64(png_bytes), "mimeType": "image/png"}`
(the `mcp.types.ImageContent` shape, already available in the installed
`mcp` package; the `@tool` decorator just returns a plain dict — that
dict isn't inert once returned, though: `claude_agent_sdk`'s `call_tool`
handler does branch on `item.get("type")` and construct real
`ImageContent`/`TextContent` objects from it, so the shape must match
exactly, `type`/`data`/`mimeType`, case-sensitive, or it raises
`KeyError`. The conclusion is unchanged — no SDK *code* needs to be
edited or added, since this existing conversion step already handles the
`image` type — but it's an existing conversion the dict must conform to,
not a case of nothing processing it at all). `visuals.py` already has a `_data_uri()` helper
(`visuals.py:149-150`, `"data:image/png;base64," + base64.b64encode(...)`)
used to embed the PDF's charts — reuse the same base64-encoding step (or
the helper itself, stripping the `data:image/png;base64,` prefix the MCP
`ImageContent.data` field doesn't want) rather than re-deriving it. Keep
the existing text block (file path) and the file-write/auto-open
behavior — additive, not a replacement.

Since the image no longer depends on local file access, move
`generate_chart` from `LOCAL_ONLY_TOOLS` into `ALL_TOOLS`, making it
reachable over the networked `/mcp/` transport. `generate_brief_report`
(PDF) is unaffected and stays `LOCAL_ONLY_TOOLS` — a PDF isn't
representable as `ImageContent`, and the "no way to retrieve the file
remotely" problem still applies to it.

**Also update the tool's description string — it goes stale otherwise.**
The literal text the calling LLM sees is the second argument to the
`@tool("generate_chart", "...", ...)` decorator (`tools.py:1975-1978`):
*"Render a beautiful standalone PNG chart (line/bar/combo) of a metric
over the last N days, for any ad-hoc trend question. Local-only:
reachable via stdio MCP clients (Claude Code/Claude Desktop on this same
machine), never over the network. Returns a local file path."* Once
`generate_chart` moves to `ALL_TOOLS`, the "Local-only ... never over the
network" claim is false and must be rewritten as part of this fix (e.g.
drop that sentence and add a short note that the response now also
includes an inline image block) — leaving it stale would tell any MCP
client deciding whether to call this tool over a networked `/mcp/`
connection that it won't work there, when it now does.

**Correction on file-write/auto-open framing:** an earlier draft of this
design said the file-write/auto-open behavior was "kept for local
clients," implying it's scoped to local calls. It isn't — `generate_chart`
is one function body reachable identically over `mcp-stdio` and the
networked `/mcp/` transport, so moving it to `ALL_TOOLS` means **every**
call, including a remote/container call over `/mcp/`, still runs
`_write_atomic` (writing to whatever disk the server process is on — the
container's filesystem for a networked call, not the caller's) and still
attempts `_auto_open`'s macOS-only `open` subprocess (already
try/caught — fails silently and harmlessly on Linux, just noisier now that
remote calls hit this path too). This is accepted as a harmless-but-vestigial
rough edge, not fixed here: the image content block is what actually
matters to a remote caller, and the extra container-local file write is
inert since nothing retrieves it. If this noise becomes a real problem
(e.g. container disk fills with orphaned chart files), that's a follow-up,
not blocking for this change.

**Not a Claude-cost path:** `generate_chart` does no LLM inference (pure
matplotlib rendering), so promoting it to the networked transport does not
require adding it to `RATE_LIMITED_PREFIXES`. Worth noting for whoever
revisits this later: `RATE_LIMITED_PREFIXES` is currently `()` (empty
tuple) since the MCP-only cutover — there's nothing to add it *to* today.
And even if it were populated, the single `/mcp/` mount means every tool
call shares one path prefix (`/mcp/`); a path-prefix limiter structurally
cannot distinguish `generate_chart` calls from any other
`mcp__fitness__*` tool call. Per-tool rate limiting, if ever needed, would
require inspecting the JSON-RPC body's `params.name`, not the URL path —
out of scope here.

**Open trade-off, not resolved here:** base64-encoding a PNG inline is a
larger response payload than today's one-line file path. MCP clients
conventionally render `image` content blocks as native image content
(image-token accounting), not raw text tokens, which is why this isn't
expected to regress the "efficiency" goal — but this is asserted from MCP
conventions, not verified against this specific SDK's client-side handling,
and should be spot-checked once implemented (see Testing Strategy).

## Fix B — Converge `get_today_status` with `daily_snapshot`

`get_today_status` (`agent/tools.py:175`) and `daily_snapshot`
(`agent/tools.py:928`, backed by `status.assemble_status()`) answer the
same question. `daily_snapshot` returns baseline-delta arrows, trend
arrows, plain-English TSB, mile-converted workouts, and saved notes;
`get_today_status` returns the same underlying `daily_metrics`/`baselines`
rows completely unformatted. Two tools for one job is exactly the
ambiguity that causes an agent to pick the weaker one and then need
follow-up calls to reconstruct what `daily_snapshot` already gives in one
shot.

**Change:** `get_today_status`'s body calls `status.assemble_status()` (same
as `daily_snapshot`) instead of its own raw query. Keep the tool name and
registration — any existing client/prompt calling it by name keeps working,
it just gets the richer payload.

**Also update the tool's description string — it goes stale otherwise, the
same treatment Fix A already gets for `generate_chart`.** The literal text
the calling LLM sees is the second argument to the
`@tool("get_today_status", "...", ...)` decorator (`tools.py:170-174`):
*"Today's metrics + last 7 days alongside the latest 60-day baselines. Call
this first when assessing recovery or making 'should I train hard'
decisions."* This describes the CURRENT raw, unformatted shape. Once the
body delegates to `assemble_status()`, the actual payload gains
baseline-delta arrows, trend arrows, plain-English TSB, mile-converted
workouts, and saved notes — the same richer shape `daily_snapshot`'s own
description (`tools.py:920-925`) already enumerates. The description string
must be rewritten as part of this fix to reflect that richer payload (e.g.
mirroring `daily_snapshot`'s description, or explicitly noting the two
tools now return equivalent payloads) — leaving it stale would tell the
calling LLM this tool still returns bare baseline numbers when it now
returns the same enriched payload as `daily_snapshot`.

**Breaks existing tests that pin the current raw shape — these are known
casualties, not a side effect to discover later.** Two tests assert on
`get_today_status`'s *current* unformatted shape and must be rewritten
(not just left to fail) as part of implementing this fix:
- `tests/test_tools.py:90-94` (`test_get_today_status`) asserts
  `payload["recent_days"]` and `payload["current_baseline"]["ctl"] == 40.0`
  — both keys disappear once the body delegates to `assemble_status()`,
  which uses `daily_snapshot`'s field names instead (arrows, trend, plain-
  English TSB, etc). Rewrite this test to assert against
  `assemble_status()`'s actual shape, mirroring whatever
  `test_daily_snapshot`-equivalent test already covers.
- `tests/test_mcp_server.py:251-264` (`test_tool_call_returns_unwrapped_content`)
  calls `get_today_status` end-to-end through the real MCP `tools/call`
  handler and asserts `"recent_days" in payload`. This needs the same
  shape update; keep the test's actual purpose (proving the handler
  returns unwrapped `text` content, not a re-wrapped envelope) by
  asserting on a key that survives the convergence, e.g. a top-level key
  `assemble_status()` guarantees.

**Interaction to verify, not just implement:** `get_today_status` is one of
the `_READ_ONLY_TOOL_NAMES` the V1 tool-driven brief loop uses (`daily_snapshot`
is deliberately excluded from that list today, "so the brief's tool set is
identical to before" a past change). Once `get_today_status` returns
`assemble_status()`'s payload, the V1 brief loop effectively gains
`daily_snapshot`-equivalent data through the tool it already calls — this
is a behavior change for the V1 brief-generation path, not just a read-tool
cleanup.

**Correction — the shadow-run baseline cannot verify this.** An earlier
draft of this design said to check this against `tests/evals/`'s
shadow-run baseline (`scripts/shadow_run.py`) before merging. That's a
false claim: `shadow_run.py` shadow-runs the **V2** brief generator, which
is toolless (`max_turns=1`, no MCP tools at all) and builds `BriefContext`
directly in Python — it never calls `get_today_status` or any other MCP
tool, so it structurally cannot observe a change in what that tool
returns. Only the **V1** tool-driven brief loop (the
`LOCAL_FITNESS_BRIEF_V2=0` manual-rollback path) calls `get_today_status`,
and there is no eval harness that shadow-runs V1 today.

Honest verification plan for the actual risk:
- Add a targeted test (`tests/test_briefing.py` or wherever V1's loop is
  already exercised) that runs V1's read-only tool set with a fixture DB
  and asserts the resulting brief is still coherent — no crash, no
  schema-invalid output — now that `get_today_status` returns the richer
  `assemble_status()` payload instead of the old raw shape. This at least
  proves the richer payload doesn't break V1's prompt/parsing path.
- If no such harness exists for V1 in isolation (V1 is the rollback-only
  path and may genuinely have no dedicated eval fixture), say so plainly
  rather than gesturing at eval coverage that doesn't apply: **this is an
  accepted, documented residual risk** on the V1 rollback path, not a
  gap this design closes. V1 is default-off (`LOCAL_FITNESS_BRIEF_V2` is
  on by default); if Nate ever flips the rollback flag, the tool-driven
  loop will see a richer `get_today_status` payload than it did when the
  V1 prompt was last tuned, and prompt behavior on that path has not been
  re-validated by this change.

## Fix C — Mile/pace convenience fields on plan tools

`get_training_plan_status` and `get_training_plan_progress` return workout
fields in raw `target_distance_m` / `target_pace_sec_per_km` /
`actual_distance_m` / `actual_pace_sec_per_km` — meters and sec-per-km.
This is the only workout-shaped data in the tool surface that skips the
project's miles display convention; every other workout tool gets this via
`_augment_workout()`.

**Change:** add a small helper, alongside `_augment_workout` (e.g.
`_augment_plan_workout(w: dict) -> dict`), that adds `target_distance_mi` /
`actual_distance_mi` / `target_pace_min_per_mi` / `actual_pace_min_per_mi`
fields using the same `units.to_miles` / `units.format_pace_min_per_mi`
functions `_augment_workout` already uses — additive, raw fields stay
(existing consumers of the raw meters/sec-per-km fields are unaffected).
Field set is **symmetric** with the existing raw fields: both tools' raw
data carries `target_distance_m`/`actual_distance_m` as a pair (mirroring
the existing `target_pace_.../actual_pace_...` pair), so the mile
convenience fields should too — a single `distance_mi` (an earlier draft's
proposal) would silently collapse target and actual into one field and
lose information the raw data still carries.

**Must reproduce the existing `display_units()` gating split exactly, not
invent a new convention.** `_augment_workout` (`tools.py:157`) and
`status.py`'s equivalent formatting step (`status.py:180`) both gate the
`distance_mi` field behind `units.display_units() == "miles"` — a real,
tested env var (`LOCAL_FITNESS_DISPLAY_UNITS`, default `"miles"`;
`tests/test_units.py:79` covers the `"km"` case). Neither call site gates
the pace field the same way: `units.format_pace_min_per_mi` has no
internal gating of its own, and both `_augment_workout` and `status.py`
add `pace_min_per_mi` unconditionally regardless of `display_units()`. So
`_augment_plan_workout` must split the same way, not apply one uniform
gate to all four new fields:
- `target_distance_mi`/`actual_distance_mi` — added only when
  `units.display_units() == "miles"`; in `"km"` mode the keys are omitted
  entirely (not present, not `None`) — matching `_augment_workout`'s
  `if miles: ... w["distance_mi"] = ...` pattern rather than always
  setting the key and letting it be `None`.
- `target_pace_min_per_mi`/`actual_pace_min_per_mi` — added
  unconditionally, same as `_augment_workout`/`status.py` already do; no
  `display_units()` check gates these.

**Two distinct integration points, not one uniform "workout list" step**
(`get_training_plan_progress` and `get_training_plan_status` don't share a
shape here):
- `get_training_plan_progress` returns a flat `workouts` list
  (`tools.py:1530-1545`), each entry carrying both `target_distance_m` and
  `actual_distance_m` (plus the pace pair). Apply `_augment_plan_workout`
  to every entry in this list.
- `get_training_plan_status` has no workout *list* at all — it returns
  `plans.build_plan_status()`'s dict with two independently-optional
  singular fields, `today` and `last_graded` (`plans.py:894-903`), each
  produced by `plans._slim_workout()` (defined in `plans.py`, not
  `tools.py`). `_slim_workout` already strips actuals down to
  target-only (`type`, `target_distance_m`, `target_pace_sec_per_km`,
  `target_duration_sec`, `description`, `verdict` — no `actual_*` keys at
  all, `plans.py:854-866`). So integrating here means: guard for `today`/
  `last_graded` being `None` (a day that hasn't happened, or no graded day
  yet) before augmenting, and expect the helper to only ever populate
  `target_distance_mi`/`target_pace_min_per_mi` on this path — there is no
  `actual_distance_m`/`actual_pace_sec_per_km` key present to convert, not
  because of a special case in the helper, but because the upstream data
  it's given never carries actuals in the first place.

**`_build_plan_section` is NOT changed by this fix — dropping that claim.**
An earlier draft of this design said `_build_plan_section()` (the
brief-PDF-only code path in `tools.py`) duplicates this conversion logic
and should be moved onto the same shared helper. On closer inspection that
doesn't hold, for two independent reasons:
1. `_build_plan_section`'s `last_7_days` entries carry verdict-conditional
   business logic that a generic augmentation helper can't reproduce:
   `actual_mi` is forced to `None` whenever `verdict in ("pending",
   "compliant")`, regardless of whether raw `actual_distance_m` is present
   (`tools.py:1787-1791`) — this suppression is locked in by
   `tests/test_tools.py:1701`, `tests/test_visuals.py:263-267`, and
   `tests/test_plan_coach.py:22-26`'s fixtures, and is specific to what
   the brief PDF should *display* (don't show an "actual" mileage next to
   a day whose verdict says it doesn't count yet), not a units-conversion
   rule `_augment_plan_workout` should encode.
2. There may be little or no real duplication to eliminate even setting
   (1) aside: `_build_plan_section` already calls `units.to_miles(...)`
   and (elsewhere in the same function) pace formatting directly, inline,
   the same way `_augment_workout` does — it isn't re-deriving the
   conversion math, it's just not routing through a shared *named*
   helper. Forcing it onto `_augment_plan_workout` would mean stripping
   the verdict-suppression back out afterward, which is more churn than
   the "one conversion path instead of three" framing implied.

So this fix's helper applies to `get_training_plan_progress` and
`get_training_plan_status` only; `_build_plan_section`'s inline conversion
and verdict-suppression logic are left exactly as they are.

(One pre-existing inconsistency noted for completeness, now moot: 
`_build_plan_section`'s inline check uses `if target_m else None`
— truthy, so a `0` value would incorrectly be excluded — while
`units.to_miles`/`_augment_workout`'s convention is `is not None` (`0`
converts to `0.0`). Since `_build_plan_section` is untouched by this fix,
this stays exactly as-is; it's not introduced or fixed here, just flagged
in case a future change to `_build_plan_section` itself wants to pick it
up.)

## API Surface

- `generate_chart(args) -> {"content": [{"type": "text", "text": json({"path": str})}, {"type": "image", "data": str (base64), "mimeType": "image/png"}]}` — was: text-only, single content block.
- `generate_chart` moves from `LOCAL_ONLY_TOOLS` to `ALL_TOOLS` (reachable via both `mcp-stdio` and the networked `/mcp/` transport). File-write/auto-open side effects are unchanged and transport-agnostic (run on every call, local or remote — see Fix A's framing correction above). The tool's `@tool` description string (`tools.py:1975-1978`) is rewritten to drop the now-false "Local-only ... never over the network" claim (see Fix A above).
- `get_today_status(_args) -> dict` — return shape becomes identical to `status.assemble_status()`'s output (today/recent-with-arrows/baseline/trend/notes), not `{today, recent_days, current_baseline}` as today. Tool name, registration, and `_READ_ONLY_TOOL_NAMES` membership unchanged. The tool's `@tool` description string (`tools.py:170-174`) is rewritten to drop the now-stale "recent days alongside baselines" raw-shape framing and reflect the richer `assemble_status()` payload, mirroring `daily_snapshot`'s description (`tools.py:920-925`) — see Fix B above.
- `get_training_plan_progress`'s workout dicts gain `target_distance_mi` / `actual_distance_mi` fields **only when `units.display_units() == "miles"`** (omitted entirely — not `None` — in `"km"` mode, matching `_augment_workout`'s gating) and `target_pace_min_per_mi` / `actual_pace_min_per_mi` fields **unconditionally** (`None` when the underlying raw value is `None`, same convention as `_augment_workout`; not gated by `display_units()`, matching `format_pace_min_per_mi`'s lack of internal gating). All existing fields unchanged.
- `get_training_plan_status`'s `today`/`last_graded` dicts (when not `None`) gain `target_distance_mi` (miles-mode only, same gate as above) / `target_pace_min_per_mi` (unconditional) only — there's no `actual_*` raw data on this path for the helper to convert (`plans._slim_workout` never includes it). All existing fields unchanged.
- New private helper `_augment_plan_workout(w: dict) -> dict` in `agent/tools.py`, used by `get_training_plan_progress` (per list entry) and `get_training_plan_status` (on `today`/`last_graded` individually, guarding `None`). Reproduces `_augment_workout`'s exact `display_units()` gating split (distance gated, pace not). **Not** used by `_build_plan_section`, which keeps its existing inline conversion and verdict-suppression logic unchanged (see Fix C's correction above).

## Invariants

**Checkable by inspection:**
- `generate_chart` is in `ALL_TOOLS`, not in `LOCAL_ONLY_TOOLS`.
- `generate_brief_report` remains in `LOCAL_ONLY_TOOLS`.
- `generate_chart`'s `@tool` description string no longer contains "local-only" / "never over the network" language.
- `get_today_status`'s body calls `assemble_status()` (no independent raw-query implementation left).
- `get_today_status`'s `@tool` description string is no longer the current raw-shape text ("Today's metrics + last 7 days alongside the latest 60-day baselines...") — it reflects (or explicitly references) the richer `assemble_status()` payload, matching `daily_snapshot`'s description.
- `_build_plan_section` is unchanged — still calls its own inline `units.to_miles`/`format_pace_min_per_mi` conversions and verdict-suppression logic, does NOT call `_augment_plan_workout`.
- `_augment_plan_workout` gates `target_distance_mi`/`actual_distance_mi` behind `units.display_units() == "miles"` and does NOT gate `target_pace_min_per_mi`/`actual_pace_min_per_mi` — same split as `_augment_workout`.

**Testable:**
- `generate_chart`'s response `content` list contains exactly one `type: "image"` block with `mimeType == "image/png"` and base64-decodable `data`.
- `generate_chart`'s registered tool description (as found in `ALL_TOOLS`) does not contain the strings "local-only" or "never over the network" (case-insensitive).
- `get_today_status(...)` output equals `daily_snapshot(...)` output for the same DB state.
- `get_today_status`'s registered tool description (as found in `ALL_TOOLS`) is no longer identical to today's literal string ("Today's metrics + last 7 days alongside the latest 60-day baselines...") and instead matches, or explicitly references, `daily_snapshot`'s description text — testable if a reasonable equivalence check is chosen (e.g. string equality with `daily_snapshot`'s description, or a substring shared with it that isn't present in today's string).
- In the default `"miles"` display-units mode, for a plan workout with known `target_distance_m`/`actual_distance_m`/`target_pace_sec_per_km`/`actual_pace_sec_per_km`, `get_training_plan_progress` returns `target_distance_mi == units.to_miles(target_distance_m)`, `actual_distance_mi == units.to_miles(actual_distance_m)`, and the matching formatted pace strings for both target and actual.
- With `LOCAL_FITNESS_DISPLAY_UNITS=km`, the same `get_training_plan_progress` call omits `target_distance_mi`/`actual_distance_mi` from the returned dicts entirely (keys absent, not `None`), while `target_pace_min_per_mi`/`actual_pace_min_per_mi` are still present and correctly computed — the pace fields are never gated by `display_units()`.
- In the default `"miles"` mode, `get_training_plan_status`'s `today`/`last_graded` (when present) return `target_distance_mi`/`target_pace_min_per_mi` computed the same way, and no `actual_distance_mi`/`actual_pace_min_per_mi` keys at all (there's no raw actual data to derive them from). In `"km"` mode, `target_distance_mi` is additionally absent (same gate as `get_training_plan_progress`) while `target_pace_min_per_mi` remains present.
- A plan workout with `target_distance_m is None` (or `actual_distance_m is None`) gets the corresponding `*_mi` field as `None` in `"miles"` mode (no crash), matching `_augment_workout`'s null-handling convention (`is not None` check, not truthy).

## Testing Strategy

- Unit tests in `tests/test_tools.py`: Fix A (image content block shape/decodability; the description-string assertion below).
- Unit tests in `tests/test_plan_tools.py`: Fix C (mile/pace field correctness + null handling on both `get_training_plan_progress` and `get_training_plan_status`, plus a `LOCAL_FITNESS_DISPLAY_UNITS=km` case per workout asserting `*_distance_mi` keys are absent while `*_pace_min_per_mi` keys are still present and correct).
  **Correction — an earlier draft of this doc misdirected these tests.** It said to add Fix C's tests to `tests/test_tools.py`, "extending existing coverage patterns already used for `_augment_workout`." That's wrong on two counts: (a) `tests/test_plan_tools.py` is the actual, pre-existing dedicated test file for `get_training_plan_progress`/`get_training_plan_status` (zero references to either tool exist in `test_tools.py`); (b) the only mile/pace assertions in `test_tools.py` (around line 1692-93) test `_build_plan_section`, which Fix C explicitly excludes from this change — not a real precedent to extend. New Fix C tests belong in `tests/test_plan_tools.py`, alongside the existing `test_progress_*`/`test_status_*` coverage for these two tools.
- Add a test asserting `generate_chart`'s registered description (via `agent_tools.ALL_TOOLS`) no longer contains "local-only" / "never over the network" — this is the part of Fix A that's easy to ship as a stale string if only the `LOCAL_ONLY_TOOLS`/`ALL_TOOLS` membership is tested.
- **Fix A breaks 3 existing tests that pin the current `ALL_TOOLS`/`LOCAL_ONLY_TOOLS` boundary as an invariant — these must be rewritten as part of this change, not left failing or deleted without replacement** (a 4th test touches the same boundary but doesn't break — see the note below the list):
  - `tests/test_smoke.py:44` — `assert len(agent_tools.ALL_TOOLS) == 33` hardcodes the current tool count; bump to 34 (and update the comment explaining the count, matching the file's existing convention of annotating each count change).
  - `tests/test_tools.py:1593-1600` — `test_generate_brief_report_and_generate_chart_excluded_from_all_tools` (labeled INV-4) asserts `local_only_names == {"generate_brief_report", "generate_chart"}` and `"generate_chart" not in all_names`. Rewrite to assert `generate_chart` IS in `ALL_TOOLS` now, `LOCAL_ONLY_TOOLS` shrinks to `{"generate_brief_report"}` only, and `generate_brief_report` (not `generate_chart`) remains excluded from `ALL_TOOLS`.
  - `tests/test_mcp_server.py:367-385` — `test_mcp_http_transport_excludes_local_only_tools` (labeled INV-T9, a live HTTP-transport test) asserts a real `/mcp/` `tools/list` call excludes both `generate_brief_report` and `generate_chart`. Rewrite so it asserts `generate_chart` IS now reachable over `/mcp/` (present in the `tools/list` response) while `generate_brief_report` still is not — this test's whole point (proving local-only tools don't leak over the network) should still hold for the one tool that's actually local-only after this change.
  - `tests/test_mcp_server.py:388-397` — `test_build_server_with_local_only_tools_serves_both` (labeled INV-T10) is a fourth test touching this boundary. It still *passes* numerically after Fix A (it asserts `build_server(extra_tools=LOCAL_ONLY_TOOLS)` serves `ALL_TOOLS | {"generate_brief_report", "generate_chart"}`, and a set union is idempotent whether or not `generate_chart` is also independently in `ALL_TOOLS`), so it won't fail CI and isn't a "must rewrite or it breaks" case like the three above. But its comment ("the exact call `run_stdio()` makes ... serves both local-only tool names") becomes misleading once `generate_chart` isn't local-only anymore — it's no longer demonstrating anything specific to local-only reachability for that tool. Implementer's note: update this test's docstring/comment for accuracy as part of this change, even though it requires no assertion changes and won't be caught by CI either way.
- **Fix B breaks 2 existing tests that pin `get_today_status`'s current raw shape — same "rewrite, not just add new tests" requirement:**
  - `tests/test_tools.py:90-94` (`test_get_today_status`) — asserts `payload["recent_days"]` and `payload["current_baseline"]["ctl"] == 40.0`. Rewrite to assert against `assemble_status()`'s actual output shape for the same fixture.
  - `tests/test_mcp_server.py:251-264` (`test_tool_call_returns_unwrapped_content`) — asserts `"recent_days" in payload`. Rewrite the shape assertion to a key that survives the convergence while preserving the test's real purpose (proving the handler unwraps content correctly rather than double-wrapping it).
- `tests/test_tools.py` or `tests/test_status.py`: Fix B — assert `get_today_status()` and `daily_snapshot()` return identical payloads given the same fixture DB state.
- **Fix B's V1 brief-loop risk is NOT covered by the shadow-run baseline** (see Fix B's correction above — `scripts/shadow_run.py` only exercises the toolless V2 generator, which never calls `get_today_status`). Verification here is either a targeted test proving V1's read-only tool set still produces a coherent brief with the richer payload, or — if no V1-specific harness exists — an explicit, documented acceptance of residual risk on the V1 rollback path. Do not cite the shadow-run baseline as coverage for this risk; it is not.
- `uv run pytest -x` full suite; no new external dependencies, so no `pyproject.toml`/`uv.lock` changes needed.

## Failure modes / edge cases

- `generate_chart`: matplotlib render failure already returns `_err(...)` before reaching the file-write step — unaffected; the new image block is only added on the existing success path.
- `generate_chart` over the networked `/mcp/` transport: the file-write/auto-open still runs server-side (container disk, no-op `open` on Linux) — harmless but not useful to a remote caller; the image content block is what actually serves them. Accepted rough edge, not fixed here (see Fix A's framing correction).
- `get_today_status`/`daily_snapshot` convergence: `assemble_status()` is documented as never raising on an empty/new DB — so `get_today_status` keeps its existing "safe on fresh clone" behavior, just with richer fields.
- `get_today_status` on the V1 tool-driven brief loop (rollback path only): receives a materially richer payload than before; not verified by the eval harness (shadow-run only covers V2) — accepted residual risk on a path that's off by default.
- Plan tools: a workout with no target/actual distance (e.g. a rest day, or a day not yet run) yields the corresponding `*_distance_mi` field as `None` (in `"miles"` mode) via `_augment_plan_workout`, not a `KeyError`/crash — same null-safety convention as `_augment_workout`. In `"km"` mode the `*_distance_mi` keys are absent regardless of whether the underlying distance is `None` or a real value (the gate is checked first). Pace fields (`*_pace_min_per_mi`) follow `format_pace_min_per_mi`'s own null/zero guard unconditionally, independent of `display_units()`. `get_training_plan_status`'s `today`/`last_graded` being `None` themselves (no plan day matches, or nothing graded yet) is guarded before calling the helper at all.
