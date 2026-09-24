"""Pure PRESS evening email: one compact digest, shared with plain text.

Inline styles and presentation tables survive stripped email CSS. No images,
web fonts, markdown, scripts or remote assets are needed to read the message.
"""
from __future__ import annotations

import html
from typing import TYPE_CHECKING

from .schemas import Brief

if TYPE_CHECKING:
    from .email_digest import Digest

EMAIL_MAX_WIDTH_PX = 560


def chart_cid(index: int | str) -> str:
    """Stable MIME naming retained for mailer's generic image support."""
    return f"chart{index}"


def subject_for(brief: Brief) -> str:
    """Keep dated subjects so separate evenings do not collapse into a thread."""
    return f"Evening Brief · {brief.date}"


def _css(**decls: str) -> str:
    return ";".join(f"{k.replace('_', '-')}:{v}" for k, v in decls.items() if v)


def build_html(digest: Digest, theme: dict) -> str:
    c, f = theme["colors"], theme["fonts"]
    escape = html.escape
    label = _css(font_family=f["mono_stack"], font_size="10px", line_height="1.5",
                 letter_spacing="0.06em", text_transform="uppercase", color=c["dim"])
    body = _css(font_family=f["serif_stack"], font_size="16px", line_height="1.45",
                color=c["ink"])
    headline = _css(font_family=f["display_stack"], font_size="38px", font_weight="900",
                    line_height="1.06", letter_spacing="-0.03em", color=c["ink"],
                    margin="8px 0 0", overflow_wrap="anywhere", word_wrap="break-word")
    section = _css(font_family=f["display_stack"], font_size="14px", font_weight="800",
                   line_height="1.35", color=c["ink"], margin="0 0 8px")
    stat_cells = "".join(
        f'<td width="33.33%" valign="top" style="padding:0 4px 0 0">'
        f'<p style="{_css(font_family=f["mono_stack"], font_size="19px", line_height="1.3", color=c["ink"], margin="0", overflow_wrap="anywhere", word_wrap="break-word")}">{escape(value)}</p>'
        f'<p style="{label};margin:5px 0 0">{escape(name)}</p></td>'
        for value, name in digest.stats
    )
    tomorrow = "".join(
        f'<p style="{body};margin:0 0 4px">{escape(line)}</p>'
        for line in digest.tomorrow
    )
    activity_note = (f'<p style="{body};font-style:italic;color:{c["dim"]};margin:8px 0 0">'
                     f'{escape(digest.activity_note)}</p>') if digest.activity_note else ""
    tomorrow_note = (f'<p style="{_css(font_family=f["serif_stack"], font_size="13px", line_height="1.4", color=c["dim"], margin="8px 0 0")}">'
                     f'{escape(digest.tomorrow_note)}</p>') if digest.tomorrow_note else ""
    # Normal messages spend no accent; a critical takeaway earns exactly one.
    insight_color = c["accent"] if digest.critical else c["ink"]
    insight = (f'<h2 style="{section};color:{insight_color}">{escape(digest.insight_label)}</h2>'
               f'<p style="{body};margin:0">{escape(digest.insight)}</p>') if digest.insight else ""
    return f'''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>Your daily TL;DR · {escape(digest.date)}</title>
</head>
<body style="margin:0;padding:0;background:{c['paper']};color:{c['ink']}">
<div aria-hidden="true" style="display:none;max-height:0;overflow:hidden;mso-hide:all">{escape(digest.headline)} Tomorrow: {escape(' '.join(digest.tomorrow))}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{c['paper']};table-layout:fixed">
<tr><td align="center">
<!--[if mso]><table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:{EMAIL_MAX_WIDTH_PX}px;table-layout:fixed;background:{c['paper']}">
<tr><td style="padding:28px 22px 24px;word-wrap:break-word;overflow-wrap:anywhere">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="table-layout:fixed;border-top:6px solid {c['rule']}">
    <tr><td style="{label};padding:12px 0 0">LOCAL FITNESS</td><td align="right" style="{label};padding:12px 0 0">{escape(digest.date)}</td></tr>
  </table>
  <p style="{_css(font_family=f['serif_stack'], font_style='italic', font_size='17px', color=c['dim'], margin='26px 0 0')}">Your daily TL;DR</p>
  <h1 style="{headline}">{escape(digest.headline)}</h1>
  {activity_note}
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="table-layout:fixed;margin:24px 0;border-top:1px solid {c['rule']};border-bottom:1px solid {c['rule']}">
    <tr><td style="padding:16px 0"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="table-layout:fixed"><tr>{stat_cells}</tr></table></td></tr>
  </table>
  {insight}
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="table-layout:fixed;margin:20px 0 0;border-top:1px solid {c['paper_elevated']}"><tr><td style="padding:18px 0 0">
    <h2 style="{section}">{escape(digest.tomorrow_label)}</h2>
    {tomorrow}{tomorrow_note}
  </td></tr></table>
  <p style="{label};letter-spacing:0;text-transform:none;margin:24px 0 0">{escape(digest.provenance)}</p>
</td></tr></table>
<!--[if mso]></td></tr></table><![endif]-->
</td></tr></table>
</body></html>'''


def build_text(digest: Digest) -> str:
    """The same concise facts and qualifications for watches and text clients."""
    return "\n".join(line for line in digest.lines() if line)
