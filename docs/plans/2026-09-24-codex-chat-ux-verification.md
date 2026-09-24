# Chat UX verification

Reviewed plan commit: `ff69780`.
Implementation branch: `feature/codex-chat-ux`, based on local `dev` at `614f7dd`.

## Result

The three everyday read tools now provide deterministic Markdown alongside their
complete structured payloads on external MCP. JSON mode and internal SDK output
retain the JSON text contract. Snapshot dates/units, provisional values, plan
windows, draft state, projection uncertainty and shared daily actuals stay explicit.
Charts carry metric/date titles, calendar spacing, gap breaks and visible isolated
points. Conversation guidance routes narrow questions to smaller tools and reuses
returned data. Scheduled V2 prompt builders are unchanged.

## Checks

| Check | Observed result |
|---|---|
| `uv run --offline pytest -x` | 2,917 passed, 6 timed benchmarks skipped; 95.30% coverage against the 85% gate |
| `uv run --no-sync ruff check .` | Passed |
| `uv run --no-sync python scripts/score_prompt.py` | 11/11 passed |
| `git diff --check` | Passed |
| Actual stdio and HTTP journeys | Markdown and exact structured-payload parity, legacy JSON, invalid formats, full/default windows and real PNG bytes verified |
| Database-open checks | Each new external view still uses exactly one connection, matching JSON mode |
| `docker build -t local-fitness:codex-chat-ux .` | Passed, including the Dockerfile's real PDF render |
| Isolated `docker run --rm --network none` smoke | Production MCP adapter returned readable empty snapshot plus structured metrics; no host mounts |
| Prompt budget | Default hardass prompt without notes: 12,897 characters, below 13,000 |
| Visual inspection | Synthetic gap and singleton chart PNGs inspected; title, labels, marks and missing-day gap visible |

Test environment explicitly selected legacy preference/journal backends, preventing
the host's opted-in vault configuration from entering isolated tests. macOS PDF
checks used `DYLD_LIBRARY_PATH=/opt/homebrew/lib`; uv/matplotlib/font caches used
writable temporary directories. An initial PDF check lacked that native-library
path, and a first full-suite run inherited the host memory backend; both passed
after environment isolation. Existing Starlette TestClient deprecation warning
remains. No live model, Garmin, email or calendar calls were made by the tests.

The exact work-count test intentionally changes 85 to 86 rounding-container calls:
the one new container is `workout_window`; date strings and its boolean add no
leaf calls. The source data queries, classification work and grading are unchanged.

## Local performance comparison

Compared the reviewed-plan tree to the implementation on the same Mac and day,
with the same fabricated database, empty brief directory and notes path. Ran
`pytest tests/test_perf_benchmarks.py --benchmark-only --no-cov
--benchmark-min-rounds=5 --benchmark-max-time=0.2 --benchmark-json=...` in each tree.
Each run passed all six timed benchmarks; 18 structural tests are skipped by that
mode and passed in the full suite. The first comparison exposed a different local
brief-directory input; the reported comparison isolates it in both trees.

| Path | Before min (ms) | After min (ms) | Change |
|---|---:|---:|---:|
| Brief context | 1.102 | 1.081 | -1.9% |
| Plan progress | 1.267 | 1.261 | -0.5% |
| Plan status | 0.806 | 0.808 | +0.3% |
| Brief plan section | 0.728 | 0.736 | +1.0% |
| Daily snapshot | 0.422 | 0.425 | +0.7% |
| Report inputs and grading | 0.653 | 0.653 | +0.1% |

The Markdown renderer itself measured 0.005–0.182 ms per view on the synthetic
fixture. Serialized result envelopes grow: snapshot 3,802→4,113 bytes, status
1,005→1,541, default progress (40 sessions) 11,869→18,210, full progress (84
sessions) 24,795→37,276. This is a readability tradeoff, not a token reduction or
an observed end-to-end Codex speedup. Narrow questions should use status; JSON mode
remains available. No committed benchmark baseline was changed.

## Review and limits

The local diff was reviewed against the plan: no new tool names, dependencies,
network paths, grading changes, private fixtures, secrets or unrelated edits.
Missing settled values with provisional readings remain distinct from no data;
double-day totals appear once, pending remains pending, and render failures retain
the source JSON. Both transport paths and the existing PDF layout tests pass.

No browser was available after Browser runtime discovery, so native Codex layout
was not visually verified. Chart images were inspected directly and the actual
stdio protocol was exercised. Linux's committed 15% benchmark gate remains a CI
check; local timings are not a substitute. The live deployment is unchanged:
local-dev ends at a draft PR, so no merge, release or rebuild from `dev` occurred.
