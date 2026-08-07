"""PRESS-styled HTML email for the evening brief.

The print pipeline (``visuals.py``) and this module render the same
``Brief``, but they target renderers with almost nothing in common, so this
is a sibling rather than a reuse of ``_build_html``:

* **WeasyPrint honours a stylesheet; Gmail does not.** A ``<style>`` block
  survives Gmail's desktop webmail and is stripped elsewhere, so every rule
  here rides an inline ``style=`` attribute. That includes the fragments the
  markdown library emits, which arrive unstyled — ``_style_fragment`` is what
  puts the PRESS grammar back onto them.
* **No ``@font-face``.** ``branding``'s ``mono_file`` data-URI trick is exactly
  what mail clients drop. The three ``*_stack`` values are already plain font
  stacks, so the voice degrades to the reader's system faces instead of
  vanishing.
* **No ``data:`` URIs.** Gmail refuses to render them. Charts are referenced by
  ``cid:`` and the bytes are attached by ``mailer.build_message`` — this module
  never sees a PNG, only the content-id string, which is what keeps it pure.
* **No density ladder.** ``fit_one_page`` exists because a PDF page is a fixed
  budget. An email scrolls, so every takeaway renders in full and nothing is
  ever truncated — the one place the email is deliberately *more* complete
  than the PDF.
* **Single column.** The print layout's two rails assume a landscape-ish page;
  this is read on a phone.

Pure by contract: no I/O, no DB, no network, no clock. Everything it needs
arrives as an argument, which is what lets the tests assert on exact strings.
"""
from __future__ import annotations

import html
import re
from datetime import datetime

from .schemas import Brief
from .visuals import _VERDICT_LABEL

#: Reading measure. 640px is the widest that still fits an iPhone's mail view
#: without horizontal scroll once the client's own chrome is subtracted.
EMAIL_MAX_WIDTH_PX = 640

#: Content-id used for takeaway *n*'s chart. The single definition — both the
#: ``<img src="cid:...">`` here and the attachment header in ``mailer`` derive
#: from it, so an image can't be referenced under a name nothing attaches.
def chart_cid(index: int | str) -> str:
    return f"chart{index}"


def subject_for(brief: Brief) -> str:
    """Subject line. Carries the date so Gmail threads each night separately —
    an identical subject collapses the whole habit into one runaway thread."""
    return f"Evening Brief · {brief.date}"


# --- inline-style helpers --------------------------------------------------
# Every visual decision is a dict of CSS declarations turned into one inline
# attribute. Kept as data rather than f-string soup so a tone or a table cell
# is defined once and the tests can assert on the resolved value.

def _css(**decls: str) -> str:
    """Render CSS declarations as an inline ``style`` attribute value.

    Underscores in keys become hyphens (``font_size`` -> ``font-size``) so the
    call sites stay valid Python keyword arguments."""
    return ";".join(f"{k.replace('_', '-')}:{v}" for k, v in decls.items() if v)


def _tone_color(tone: str, theme: dict) -> str:
    """PRESS treats tone typographically, not as a palette: the accent is spent
    on trouble and nothing else, so a good day is legitimately monochrome."""
    c = theme["colors"]
    return {
        "positive": c["ink"],
        "neutral": c["ink"],
        "caution": c["dim"],
        "critical": c["accent"],
    }.get(tone, c["ink"])


#: Opening tags the markdown library emits, mapped to the inline style each
#: needs. A tag absent here renders unstyled rather than wrong — mail clients
#: have sane defaults for `<strong>`/`<em>`, and inventing rules for them would
#: only fight the reader's own settings.
def _fragment_styles(theme: dict) -> dict[str, str]:
    c, f = theme["colors"], theme["fonts"]
    return {
        "p": _css(margin="0 0 10px", font_family=f["serif_stack"],
                  font_size="15px", line_height="1.55", color=c["ink"]),
        "ul": _css(margin="0 0 10px", padding_left="20px",
                   font_family=f["serif_stack"], font_size="15px",
                   line_height="1.55", color=c["ink"]),
        "ol": _css(margin="0 0 10px", padding_left="20px",
                   font_family=f["serif_stack"], font_size="15px",
                   line_height="1.55", color=c["ink"]),
        "li": _css(margin="0 0 4px"),
        "table": _css(border_collapse="collapse", width="100%",
                      margin="0 0 12px", font_family=f["mono_stack"],
                      font_size="12px", color=c["ink"]),
        "th": _css(border_bottom=f"1px solid {c['rule']}", padding="5px 6px",
                   text_align="left", font_weight="700",
                   text_transform="uppercase", letter_spacing="0.06em",
                   font_size="10px", color=c["dim"]),
        "td": _css(border_bottom=f"1px solid {c['paper_elevated']}",
                   padding="5px 6px", text_align="left"),
        "code": _css(font_family=f["mono_stack"], font_size="12px",
                     background=c["paper_elevated"], padding="1px 4px"),
        "h1": _css(margin="14px 0 6px", font_family=f["display_stack"],
                   font_size="16px", font_weight="800", color=c["ink"]),
        "h2": _css(margin="14px 0 6px", font_family=f["display_stack"],
                   font_size="15px", font_weight="800", color=c["ink"]),
        "h3": _css(margin="12px 0 6px", font_family=f["display_stack"],
                   font_size="14px", font_weight="800", color=c["ink"]),
    }


_TAG_RE = re.compile(r"<(?P<tag>[a-z][a-z0-9]*)(?P<attrs>\s[^>]*)?>", re.IGNORECASE)


def _style_fragment(fragment: str, theme: dict) -> str:
    """Push inline styles onto markdown-generated HTML.

    The markdown library emits bare ``<p>``/``<table>``/``<td>`` tags, and a
    mail client that dropped our ``<style>`` block would render them as
    unstyled browser defaults — Times New Roman tables with no rules, beside
    hand-built sections that look right. Rewriting the opening tags is what
    keeps one grammar across both.

    A tag that already carries a ``style`` attribute is left alone: the only
    way one gets here is if a future caller styled it deliberately, and
    silently overwriting that would be the harder bug to find.
    """
    styles = _fragment_styles(theme)

    def _sub(m: re.Match) -> str:
        tag = m.group("tag").lower()
        attrs = m.group("attrs") or ""
        if tag not in styles or "style=" in attrs.lower():
            return m.group(0)
        return f'<{m.group("tag")}{attrs} style="{styles[tag]}">'

    return _TAG_RE.sub(_sub, fragment)


def _fmt_mi(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f} mi"


def _fmt_stamp(brief: Brief) -> str:
    """Human-readable generation time for the footer.

    ``generated_at`` is ``datetime.now().isoformat()``, which carries
    microseconds — "2026-08-07T06:31:20.257259" is machine provenance printed
    at a reader. Trimmed to the minute, which is all the precision that means
    anything here. A brief predating the field, or carrying a value this can't
    parse, falls back to its date rather than showing nothing.
    """
    raw = brief.generated_at
    if not raw:
        return brief.date
    try:
        return datetime.fromisoformat(raw).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw


# --- sections --------------------------------------------------------------

def _masthead_html(brief: Brief, theme: dict) -> str:
    c, f, ident = theme["colors"], theme["fonts"], theme["identity"]
    eyebrow = f"{ident['brand_line']} · EVENING BRIEF · {brief.date}"
    return f"""
      <tr><td style="{_css(padding='24px 24px 0')}">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
          <tr>
            <td style="{_css(font_family=f['mono_stack'], font_size='10px',
                             letter_spacing='0.12em', text_transform='uppercase',
                             color=c['dim'])}">{html.escape(eyebrow)}</td>
            <td align="right" style="{_css(font_family=f['mono_stack'],
                                           font_size='10px', color=c['dim'])}"
            >{html.escape(ident['byline'])}</td>
          </tr>
        </table>
        <h1 style="{_css(margin='10px 0 0', font_family=f['display_stack'],
                         font_size='30px', line_height='1.1', font_weight='900',
                         letter_spacing='-0.02em', color=c['ink'])}"
        >{html.escape(brief.user_name)}'s Brief</h1>
        <div style="{_css(border_top=f"2px solid {c['rule']}", margin='14px 0 0',
                          font_size='0', line_height='0')}">&nbsp;</div>
      </td></tr>
    """


def _takeaway_html(index: int, takeaway, has_chart: bool, theme: dict) -> str:
    import markdown as md_lib

    c, f = theme["colors"], theme["fonts"]
    accent = _tone_color(takeaway.tone, theme)
    # `details` is the only field routed through the markdown library, which
    # escapes as it renders. `headline`/`summary` are plain string-building
    # with no autoescaping underneath, so they are escaped explicitly — same
    # split, and same reasoning, as visuals._build_html.
    details = _style_fragment(
        md_lib.markdown(takeaway.details, extensions=["tables"]), theme)

    chart = ""
    if has_chart:
        chart = f"""
        <img src="cid:{chart_cid(index)}" alt="" width="{EMAIL_MAX_WIDTH_PX - 48}"
             style="{_css(display='block', width='100%',
                          max_width=f'{EMAIL_MAX_WIDTH_PX - 48}px',
                          height='auto', margin='0 0 12px',
                          border=f"1px solid {c['paper_elevated']}")}">
        """

    # The tone rides a left rule, not a fill: PRESS keeps paper flat, and a
    # tinted card background is the first thing Gmail's dark mode mangles.
    return f"""
      <tr><td style="{_css(padding='20px 24px 0')}">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
               style="{_css(border_left=f'3px solid {accent}')}">
          <tr><td style="{_css(padding='0 0 0 14px')}">
            <h2 style="{_css(margin='0 0 4px', font_family=f['display_stack'],
                             font_size='18px', line_height='1.25',
                             font_weight='800', letter_spacing='-0.01em',
                             color=c['ink'])}">{html.escape(takeaway.headline)}</h2>
            <p style="{_css(margin='0 0 12px', font_family=f['serif_stack'],
                            font_style='italic', font_size='15px',
                            line_height='1.5', color=c['dim'])}"
            >{html.escape(takeaway.summary)}</p>
            {chart}
            {details}
          </td></tr>
        </table>
      </td></tr>
    """


def _stat_tile(value: str, label: str, theme: dict) -> str:
    c, f = theme["colors"], theme["fonts"]
    return f"""
      <td width="33%" style="{_css(padding='0 8px 0 0', vertical_align='top')}">
        <div style="{_css(font_family=f['display_stack'], font_size='22px',
                          font_weight='900', line_height='1.1',
                          color=c['ink'])}">{value}</div>
        <div style="{_css(font_family=f['mono_stack'], font_size='9px',
                          letter_spacing='0.08em', text_transform='uppercase',
                          color=c['dim'], padding_top='3px')}">{label}</div>
      </td>
    """


def _plan_html(plan_section: dict | None, theme: dict) -> str:
    """Training Plan section, or "" when the caller passed None.

    Mirrors ``visuals._render_plan_section_html``'s field handling exactly —
    same payload, same suffix rules — because the two render one section and a
    divergence between them is invisible until someone reads both artifacts
    side by side. This one does no DB access either; the caller builds the
    payload."""
    if plan_section is None:
        return ""

    c, f = theme["colors"], theme["fonts"]

    if plan_section.get("days_to_race") is not None:
        tile2 = (str(plan_section["days_to_race"]), "Days to Race")
    else:
        tile2 = (html.escape(plan_section["goal_type"]), "Goal")

    walk_mi = plan_section.get("week_walk_mi") or 0
    walk_suffix = f" · +{walk_mi:.1f} walked" if walk_mi else ""
    sessions_pct = plan_section.get("sessions_adherence_pct")
    # `is not None`, not truthy — 0% of sessions is precisely the number that
    # most needs printing beside a flattering total.
    adherence_suffix = f" · {sessions_pct}% sessions" if sessions_pct is not None else ""

    tiles = (
        _stat_tile(f'{plan_section["adherence_pct"]}%',
                   f"Adherence{html.escape(adherence_suffix)}", theme)
        + _stat_tile(tile2[0], tile2[1], theme)
        + _stat_tile(str(plan_section["slips"]), "Slips", theme)
    )
    mileage = _stat_tile(
        f'{_fmt_mi(plan_section["week_actual_mi"])} / {_fmt_mi(plan_section["week_planned_mi"])}',
        f"Run mi · actual / planned{html.escape(walk_suffix)}", theme)

    today = plan_section.get("today")
    today_html = ""
    if today is not None:
        rx = html.escape(today["type"])
        if today.get("distance_mi") is not None:
            rx += f" · {today['distance_mi']:.1f} mi"
        if today.get("pace_min_per_mi"):
            rx += f" @ {html.escape(today['pace_min_per_mi'])}/mi"
        description = (
            f'<p style="{_css(margin="0 0 6px", font_family=f["serif_stack"], font_size="14px", color=c["dim"])}">'
            f'{html.escape(today["description"])}</p>'
            if today.get("description") else ""
        )
        coaching = (
            f'<p style="{_css(margin="0", font_family=f["serif_stack"], font_style="italic", font_size="15px", line_height="1.5", color=c["ink"])}">'
            f'{html.escape(today["coaching_line"])}</p>'
            if today.get("coaching_line") else ""
        )
        today_html = f"""
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
               style="{_css(background=c['paper_surface'], margin='16px 0 0')}">
          <tr><td style="{_css(padding='14px 16px')}">
            <div style="{_css(font_family=f['mono_stack'], font_size='9px',
                              letter_spacing='0.12em', text_transform='uppercase',
                              color=c['dim'], padding_bottom='6px')}">Today</div>
            <p style="{_css(margin='0 0 6px', font_family=f['display_stack'],
                            font_size='16px', font_weight='800', color=c['ink'])}">{rx}</p>
            {description}
            {coaching}
          </td></tr>
        </table>
        """

    rows = "".join(
        f"""
        <tr>
          <td style="{_css(padding='5px 6px', border_bottom=f"1px solid {c['paper_elevated']}", font_family=f['mono_stack'], font_size='12px', color=c['ink'])}">{html.escape(day["date"][5:])}</td>
          <td style="{_css(padding='5px 6px', border_bottom=f"1px solid {c['paper_elevated']}", font_family=f['mono_stack'], font_size='12px', color=c['ink'])}">{html.escape(day["type"])}</td>
          <td style="{_css(padding='5px 6px', border_bottom=f"1px solid {c['paper_elevated']}", font_family=f['mono_stack'], font_size='12px', color=c['ink'])}">{_fmt_mi(day.get("planned_mi"))}</td>
          <td style="{_css(padding='5px 6px', border_bottom=f"1px solid {c['paper_elevated']}", font_family=f['mono_stack'], font_size='12px', color=c['ink'])}">{_fmt_mi(day.get("actual_mi"))}</td>
          <td style="{_css(padding='5px 6px', border_bottom=f"1px solid {c['paper_elevated']}", font_family=f['mono_stack'], font_size='12px', font_weight='700', color=_verdict_color(day["verdict"], theme))}">{html.escape(_VERDICT_LABEL.get(day["verdict"], day["verdict"]))}</td>
        </tr>
        """
        for day in plan_section.get("last_7_days", [])
    )
    head_css = _css(padding='5px 6px', border_bottom=f"1px solid {c['rule']}",
                    text_align='left', font_family=f['mono_stack'],
                    font_size='9px', letter_spacing='0.08em',
                    text_transform='uppercase', font_weight='700', color=c['dim'])
    table_html = (
        f"""
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
               style="{_css(border_collapse='collapse', margin='16px 0 0')}">
          <tr>
            <th style="{head_css}">Date</th><th style="{head_css}">Type</th>
            <th style="{head_css}">Planned</th><th style="{head_css}">Actual</th>
            <th style="{head_css}">Verdict</th>
          </tr>
          {rows}
        </table>
        """
        if rows else ""
    )

    return f"""
      <tr><td style="{_css(padding='24px 24px 0')}">
        <div style="{_css(border_top=f"2px solid {c['rule']}", margin='0 0 14px',
                          font_size='0', line_height='0')}">&nbsp;</div>
        <h2 style="{_css(margin='0 0 12px', font_family=f['display_stack'],
                         font_size='13px', font_weight='800',
                         letter_spacing='0.1em', text_transform='uppercase',
                         color=c['ink'])}">Training Plan</h2>
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
          <tr>{tiles}</tr>
        </table>
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
               style="{_css(padding_top='14px')}">
          <tr>{mileage}</tr>
        </table>
        {today_html}
        {table_html}
      </td></tr>
    """


def _verdict_color(verdict: str, theme: dict) -> str:
    """Same PRESS rule as the print card: only a miss earns the accent."""
    c = theme["colors"]
    return {
        "done": c["ink"],
        "partial": c["dim"],
        "missed": c["accent"],
        "rest": c["dim"],
        "scheduled": c["dim"],
    }.get(verdict, c["ink"])


def _footer_html(brief: Brief, theme: dict) -> str:
    c, f = theme["colors"], theme["fonts"]
    stamp = _fmt_stamp(brief)
    return f"""
      <tr><td style="{_css(padding='24px')}">
        <div style="{_css(border_top=f"1px solid {c['paper_elevated']}",
                          margin='0 0 10px', font_size='0', line_height='0')}">&nbsp;</div>
        <p style="{_css(margin='0', font_family=f['mono_stack'], font_size='10px',
                        color=c['dim'], line_height='1.5')}"
        >Generated {html.escape(stamp)} · local-fitness</p>
      </td></tr>
    """


# --- entry points ----------------------------------------------------------

def build_html(
    brief: Brief,
    chart_indexes: set[int] | frozenset[int],
    plan_section: dict | None,
    theme: dict,
) -> str:
    """Assemble the full email body.

    ``chart_indexes`` is which takeaway positions have a chart attached — not
    the bytes. This module names the ``cid:`` and ``mailer`` attaches under the
    same name via ``chart_cid``; keeping the bytes out is what lets the whole
    layout be asserted with plain string comparisons.
    """
    c = theme["colors"]
    sections = "".join(
        _takeaway_html(i, tk, i in chart_indexes, theme)
        for i, tk in enumerate(brief.takeaways)
    )
    body = (
        _masthead_html(brief, theme)
        + sections
        + _plan_html(plan_section, theme)
        + _footer_html(brief, theme)
    )
    # An explicit paper background on BOTH the outer table and the inner one:
    # a client that ignores the outer still frames the content correctly, and
    # the reverse leaves a white gutter around warm paper.
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>{html.escape(subject_for(brief))}</title>
</head>
<body style="{_css(margin='0', padding='0', background=c['paper'])}">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="{_css(background=c['paper'], margin='0', padding='0')}">
  <tr><td align="center" style="{_css(padding='0')}">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
           style="{_css(max_width=f'{EMAIL_MAX_WIDTH_PX}px', background=c['paper'],
                        margin='0 auto')}">
      {body}
    </table>
  </td></tr>
</table>
</body>
</html>"""


def build_text(brief: Brief, plan_section: dict | None) -> str:
    """The ``text/plain`` alternative.

    Not a courtesy: a message with no plain part is scored as more
    spam-like, and it is what a watch or a screen reader gets. ``details`` is
    emitted as its original markdown, which is already the readable form.
    """
    lines = [
        f"{brief.user_name.upper()}'S BRIEF — EVENING · {brief.date}",
        "=" * 56,
        "",
    ]
    for takeaway in brief.takeaways:
        lines += [
            takeaway.headline.upper(),
            takeaway.summary,
            "",
            takeaway.details.strip(),
            "",
            "-" * 56,
            "",
        ]

    if plan_section is not None:
        lines.append("TRAINING PLAN")
        sessions_pct = plan_section.get("sessions_adherence_pct")
        adherence = f'{plan_section["adherence_pct"]}% adherence'
        if sessions_pct is not None:
            adherence += f" · {sessions_pct}% sessions"
        lines.append(adherence)
        if plan_section.get("days_to_race") is not None:
            lines.append(f'{plan_section["days_to_race"]} days to race')
        lines.append(
            f'Run mi actual/planned: {_fmt_mi(plan_section["week_actual_mi"])}'
            f' / {_fmt_mi(plan_section["week_planned_mi"])}'
        )
        lines.append(f'Slips: {plan_section["slips"]}')
        today = plan_section.get("today")
        if today is not None:
            rx = today["type"]
            if today.get("distance_mi") is not None:
                rx += f' · {today["distance_mi"]:.1f} mi'
            if today.get("pace_min_per_mi"):
                rx += f' @ {today["pace_min_per_mi"]}/mi'
            lines += ["", f"Today: {rx}"]
            if today.get("description"):
                lines.append(today["description"])
            if today.get("coaching_line"):
                lines.append(today["coaching_line"])
        lines.append("")
        for day in plan_section.get("last_7_days", []):
            lines.append(
                f'  {day["date"][5:]}  {day["type"]:<10}'
                f'  planned {_fmt_mi(day.get("planned_mi")):>8}'
                f'  actual {_fmt_mi(day.get("actual_mi")):>8}'
                f'  {_VERDICT_LABEL.get(day["verdict"], day["verdict"])}'
            )
        lines.append("")

    lines.append(f"Generated {_fmt_stamp(brief)} · local-fitness")
    return "\n".join(lines)
