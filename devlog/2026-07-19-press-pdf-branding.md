# PRESS-branded PDFs — theme-driven, local-overridable

**2026-07-19**

The brief PDF wore the sibling `budget` project's blue theme since birth.
Nate's actual brand converged on **PRESS** (his site, ghostwriter cards,
devlog covers): warm paper, ink rules, ONE orange, editorial typography.
This build makes every generated PDF/PNG theme-driven with PRESS as the
checked-in default and a local override for anyone else cloning the
public repo.

## How the brand was sourced

A recon agent swept three repos and found the canonical tokens in
`~/.claude/ghostwriter/assets/diagram.css` (`.card.press`) + the devlog
image style guide (both 2026-07-18) — and flagged two SUPERSEDED systems
to ignore (the old black/chartreuse release covers still on disk, and the
GitHub-dark diagram tokens). Palette: #F5F0E6 / #181510 / #6E675C /
#E8501F. Voices: system sans 800–900 (structure), New York/Georgia italic
(commentary), IBM Plex Mono (data).

## Decisions (with Nate)

- **PRESS-strict tones**: no green/amber/red. done/positive = ink,
  partial/caution = dim italic, rest = dim, MISSED/critical = the one
  accent, caps. A clean week renders with zero orange in the body — the
  stamp is the only accent. Bad days may show orange repeatedly: data
  honesty over signature law.
- **System mono stack default** (`ui-monospace/SF Mono/Menlo`), no font
  binaries in the repo. `fonts.mono_file` embeds a real TTF via a
  data:-URI @font-face (`_report_url_fetcher` only allows `data:`) —
  Nate's gitignored `data/brand.json` points at his devlog Plex Mono.

## Mechanics

- `agent/branding.py`: `DEFAULT_THEME` + `load_theme()` —
  `LOCAL_FITNESS_BRAND_FILE` JSON deep-merges over the default; corrupt/
  missing file logs a warning and falls back (a bad brand file must never
  kill a render); loaded per render so edits apply without restart.
- `visuals.py`: `_CSS` constant → `_build_css(theme)`; masthead replaces
  the header banner; ruled sections replace tinted rounded cards; PRESS
  numerals replace filled stat tiles; charts (`render_chart_png`) draw on
  paper with ink marks + accent trend line. ASCII terminal charts keep
  their emoji heat ramp — PRESS is the print brand.

## Gotchas

- pdfplumber extracts CSS-transformed text: `text-transform: uppercase`
  on verdicts means tests must assert "MISSED", not "missed".
- A layout `<table>` in the masthead broke a "no `<tr>` in card HTML"
  regression pin — the assertion now counts exactly one `<tr>` (the
  masthead) and checks it precedes the first card.
- Asserting order against `"signal-card"` matches the CSS *selector*
  before the body — anchor on `class="signal-card` instead.
- WeasyPrint paints the page canvas (incl. margins) from the root
  element's background — `html { background: paper }`, no @page hack.

Verified per the screenshot rule: real-brief renders (default PRESS,
scratch override with different palette/identity, themed combo chart)
eyeballed by Nate before shipping.
