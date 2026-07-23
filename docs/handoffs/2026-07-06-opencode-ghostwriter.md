# Handoff: LinkedIn post about opencode migration

**Mode:** Continuation (draft written, card rendered, awaiting approval)

## Goal

Get Nate's explicit approval on a drafted LinkedIn post + card about migrating his local-fitness AI coach from the Anthropic API to opencode with local Gemma 4 on Ollama, then publish.

## State snapshot

- **Project:** `local-fitness` (branch `dev`, SHA `adb2f5d`)
- **Skill:** `ghostwriter` at `/Users/natejswenson/.claude/skills/ghostwriter`
- **No uncommitted changes** in the ghostwriter repo (drafts and images are gitignored by design)
- **Was interrupted at:** "save where we are and clear context" — post is drafted, card is rendered, Nate hasn't said publish/edit/scrap yet on the final revision

## Next concrete action

Show Nate the final draft + card and ask: "Publish this to LinkedIn, edit it, or scrap it?"

If he says **publish**: run the dry-run first, then publish (the draft's sources.json passes verify_sources.py).
If he says **edit**: apply his edits, re-verify sources if claims change, re-render the card if needed, re-show.
If he says **scrap**: nothing to do.

## What exists

### LinkedIn post draft
- **Path:** `drafts/2026-07-06-opencode-agent-loop.md` in the ghostwriter skill dir
- **Slug:** `2026-07-06-opencode-agent-loop`
- **Content:**
  ```
  One year. AI fitness coach on the Anthropic API.

  Now it runs in opencode with Gemma 4 on Ollama. Cost went from per-token
  billing to zero. The tool is MIT licensed. Inference is local, ~9.6GB model
  on localhost:11434.

  Two model configs in opencode.jsonc: gemma4 with reasoning enabled,
  gemma4-agent with 32k context and thinking disabled for tool calls. Both
  hit 0.0% invention rate across four shadow-run configurations. 12/12 schema
  compliance with structured output. 792 tests passing.

  The whole migration is 731 lines across 10 files. Swapping models is one
  line in the config.
  ```
- **Sources:** `drafts/2026-07-06-opencode-agent-loop.sources.json` — 3 distinct live hosts, passes `verify_sources.py`

### Card image
- **HTML source:** `images/2026-07-06-opencode-agent-loop.html` (brief template, light design system, portrait 1200x1500)
- **Rendered PNG:** `images/2026-07-06-opencode-agent-loop.png` (317K)
- **Card structure:** Eyebrow "Cost migration · agent tooling" → Headline "From paid API to zero-cost inference." → Lead → Before/After panel (server icon → cpu icon) → Band "Model-agnostic harness. Free tool. Local inference." → Stats row (0.0% invention · 12/12 schema · 792 tests) → Caption "731 lines across 10 files · swap models with one config line"

### Editing history (how we got here)
1. First draft: third-person radar post about opencode hitting 183k stars
2. User wanted personal angle about transitioning to opencode
3. User wanted cost focus and local-model exploration instead of plugin detail
4. User said post sounded AI-written with filler — rewrite with hard data from his code
5. Final version (above): tight, data-dense, every line carries a real number
6. User asked for a card → rendered brief card with before/after concept + stats

## Key data from Nate's actual code used in the post

All numbers are real from his repo and config — never fabricated:
- **~9.6GB model** — Gemma 4 pulled via Ollama (from the Jul 5 post draft)
- **Two model configs** — `gemma4` (reasoning enabled) and `gemma4-agent` (32k ctx, thinking disabled) from `~/.config/opencode/opencode.jsonc`
- **0.0% invention rate** across 4 shadow-run configs — from commit `990b65e`
- **12/12 schema compliance** with structured output — same commit
- **792 tests passing** — same commit
- **731 lines across 10 files** — same commit
- **MCP server** — `uv run fitness mcp-stdio` in opencode.jsonc
- **Plugin** — `fitness-chart-relay.js` (49 lines) in `~/.config/opencode/plugins/`

## Standing directives (from voice profile + voice notes)

- No em dashes — use commas or semicolons
- No rhetorical fragment lists ("No X. No Y. No Z.")
- No reflexive closing questions
- No clever-symmetry payoff closers ("X is the ceiling… the floor")
- End on the last real, concrete point — don't tack on a summarizing flourish
- Tight (~50-120 words default; this one is ~90)
- No links in the post body (use first comment)
- No hashtags unless the voice profile says otherwise (it doesn't)
- Never publish without explicit approval of the exact text

## Recovery pointers

- **Ghostwriter skill:** `/Users/natejswenson/.claude/skills/ghostwriter/SKILL.md`
- **Voice profile:** `voice/voice-profile.md` (same dir)
- **Voice notes (overrides):** `voice/voice-notes.md` (same dir)
- **Algorithm guide:** `voice/algorithm.md` (same dir)
- **Publish command:** `scripts/linkedin_post.py --file drafts/2026-07-06-opencode-agent-loop.md` (with optional `--image` for the card)
- **Dry-run check:** `scripts/linkedin_post.py --file drafts/2026-07-06-opencode-agent-loop.md --dry-run`
- **Nate's opencode config:** `~/.config/opencode/opencode.jsonc`

## Open questions / decisions deferred

- Whether to include the card image when publishing (it was rendered and shown but Nate hadn't decided)
- Whether Nate wants to post this now or wait (he said "save where we are")
