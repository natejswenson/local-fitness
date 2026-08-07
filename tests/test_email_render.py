"""Tests for the evening brief's HTML email rendering.

The bar these hold to: every assertion pins a value that would CHANGE if the
renderer broke. The email-safety rules (no ``data:`` URIs, no ``<style>``
block, no ``@font-face``, inline styles on markdown output) are the ones worth
guarding hardest — each of them fails *silently* in Gmail, rendering a message
that looks fine in a browser preview and broken in the inbox, which is exactly
the class of bug a test has to catch instead of a person.
"""
from __future__ import annotations

import re

import pytest

from local_fitness.agent import email_render
from local_fitness.agent.branding import DEFAULT_THEME
from local_fitness.agent.schemas import Brief

THEME = DEFAULT_THEME


def make_brief(**over) -> Brief:
    payload = {
        "date": "2026-08-07",
        "user_name": "Nate",
        "generated_at": "2026-08-07T06:31:20.257259",
        "takeaways": [
            {
                "headline": "Long run 9mi today",
                "summary": "TSB +5.4 says the legs are loaded.",
                "tone": "positive",
                "metric": {"metric": "tsb", "days": 30},
                "details": "Plan: 9mi easy-steady.\n\n| Metric | Today |\n| --- | --- |\n| RHR | 54 |",
            },
            {
                "headline": "CTL -5.6% in 14 days",
                "summary": "Fitness at 57.8, down from 61.2.",
                "tone": "critical",
                "metric": None,
                "details": "Eight runs in 14 days sounds like consistency.",
            },
        ],
    }
    payload.update(over)
    return Brief.model_validate(payload)


PLAN = {
    "adherence_pct": 83,
    "sessions_adherence_pct": 83,
    "rest_days_counted": 1,
    "goal_type": "10k",
    "days_to_race": 42,
    "week_planned_mi": 36.0,
    "week_actual_mi": 14.2,
    "week_walk_mi": 6.0,
    "slips": 2,
    "today": {
        "type": "long",
        "distance_mi": 9.0,
        "pace_min_per_mi": "9:23",
        "description": "Long run 9mi @ easy-steady.",
        "coaching_line": "Yesterday you hit the session clean.",
    },
    "last_7_days": [
        {"date": "2026-08-07", "type": "long", "planned_mi": 9.0,
         "actual_mi": None, "verdict": "scheduled"},
        {"date": "2026-08-01", "type": "easy", "planned_mi": 4.0,
         "actual_mi": 0.0, "verdict": "missed"},
    ],
}


# --- subject ---------------------------------------------------------------

def test_subject_carries_the_date():
    # Exact string: an identical subject every night collapses the habit into
    # one Gmail thread, which is the whole reason the date is in there.
    assert email_render.subject_for(make_brief()) == "Evening Brief · 2026-08-07"


def test_subject_tracks_the_briefs_own_date_not_a_constant():
    assert email_render.subject_for(make_brief(date="2026-12-25")).endswith("2026-12-25")


# --- email-client safety ---------------------------------------------------

def test_no_data_uris_anywhere():
    # Gmail refuses to render `data:` images. If a chart ever regresses to the
    # PDF's `_data_uri` path, the email silently loses every figure.
    html = email_render.build_html(make_brief(), {0}, PLAN, THEME)
    assert "data:" not in html


def test_no_style_block_and_no_font_face():
    # A <style> block survives Gmail desktop and is stripped elsewhere;
    # @font-face is dropped everywhere. Either one means the message renders
    # unstyled for some readers and correct for others.
    html = email_render.build_html(make_brief(), {0}, PLAN, THEME)
    assert "<style" not in html
    assert "@font-face" not in html


def test_layout_is_tables_not_flexbox_or_grid():
    html = email_render.build_html(make_brief(), {0}, PLAN, THEME)
    assert "display:flex" not in html
    assert "display:grid" not in html
    assert "<table" in html


# --- charts ----------------------------------------------------------------

def test_chart_img_is_emitted_only_for_indexes_with_a_chart():
    html = email_render.build_html(make_brief(), {0}, PLAN, THEME)
    assert 'src="cid:chart0"' in html
    # Takeaway 1 has metric=None, so no chart was rendered for it — referencing
    # a cid with nothing attached shows a broken-image icon in the client.
    assert 'src="cid:chart1"' not in html


def test_no_chart_refs_when_nothing_was_rendered():
    html = email_render.build_html(make_brief(), set(), PLAN, THEME)
    assert "cid:" not in html


def test_chart_cid_is_stable_and_index_keyed():
    # The single definition shared with mailer.build_message. A change here
    # without a matching change there breaks every image.
    assert email_render.chart_cid(0) == "chart0"
    assert email_render.chart_cid("11") == "chart11"


# --- content ---------------------------------------------------------------

def test_every_takeaway_renders_headline_and_summary():
    brief = make_brief()
    html = email_render.build_html(brief, {0}, PLAN, THEME)
    for tk in brief.takeaways:
        assert tk.headline in html
        assert tk.summary in html


def test_details_markdown_table_becomes_a_styled_html_table():
    html = email_render.build_html(make_brief(), set(), None, THEME)
    # The markdown extension ran...
    assert "<table" in html and "<td" in html
    # ...and the emitted cells carry inline styling rather than browser defaults.
    assert re.search(r'<td[^>]*style="[^"]*padding:5px 6px', html)


def test_headline_is_html_escaped():
    brief = make_brief()
    brief.takeaways[0].headline = '<script>alert("x")</script> & more'
    html = email_render.build_html(brief, set(), None, THEME)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp; more" in html


def test_generated_stamp_is_trimmed_to_the_minute():
    html = email_render.build_html(make_brief(), set(), None, THEME)
    assert "2026-08-07 06:31" in html
    # Microsecond provenance is machine noise printed at a human.
    assert "06:31:20.257259" not in html


@pytest.mark.parametrize(
    "generated_at,expected",
    [
        ("2026-08-07T06:31:20.257259", "2026-08-07 06:31"),
        ("2026-08-07T06:31:20", "2026-08-07 06:31"),
        (None, "2026-08-07"),            # pre-2026-04-27 brief: fall back to date
        ("not-a-timestamp", "not-a-timestamp"),  # unparseable: show it, don't crash
    ],
)
def test_fmt_stamp_handles_every_generated_at_shape(generated_at, expected):
    assert email_render._fmt_stamp(make_brief(generated_at=generated_at)) == expected


# --- tone / verdict color --------------------------------------------------

def test_tone_colors_spend_the_accent_only_on_critical():
    # PRESS's accent law: one loud color, spent on trouble. A brief with no
    # critical takeaway is legitimately monochrome, and that is not a bug.
    c = THEME["colors"]
    assert email_render._tone_color("critical", THEME) == c["accent"]
    assert email_render._tone_color("positive", THEME) == c["ink"]
    assert email_render._tone_color("neutral", THEME) == c["ink"]
    assert email_render._tone_color("caution", THEME) == c["dim"]


def test_unknown_tone_falls_back_to_ink_not_accent():
    # A future tone value must not accidentally inherit the loud color.
    assert email_render._tone_color("wat", THEME) == THEME["colors"]["ink"]


def test_only_a_missed_day_earns_the_accent_in_the_week_table():
    c = THEME["colors"]
    assert email_render._verdict_color("missed", THEME) == c["accent"]
    for ok in ("done", "partial", "rest", "scheduled"):
        assert email_render._verdict_color(ok, THEME) != c["accent"]


def test_a_clean_week_uses_no_accent_at_all():
    clean = dict(PLAN, last_7_days=[
        {"date": "2026-08-06", "type": "easy", "planned_mi": 3.0,
         "actual_mi": 3.0, "verdict": "done"},
    ])
    brief = make_brief()
    for tk in brief.takeaways:
        tk.tone = "positive"
    html = email_render.build_html(brief, set(), clean, THEME)
    assert THEME["colors"]["accent"] not in html


# --- plan section ----------------------------------------------------------

def test_plan_section_is_omitted_entirely_when_none():
    html = email_render.build_html(make_brief(), set(), None, THEME)
    assert "Training Plan" not in html
    assert "Adherence" not in html


def test_plan_section_renders_tiles_today_and_week_rows():
    html = email_render.build_html(make_brief(), set(), PLAN, THEME)
    assert "Training Plan" in html
    assert "83%" in html
    assert "42" in html and "Days to Race" in html
    assert "14.2 mi / 36.0 mi" in html
    assert "long · 9.0 mi @ 9:23/mi" in html
    assert "Yesterday you hit the session clean." in html
    assert "08-07" in html and "08-01" in html


def test_goal_tile_replaces_days_to_race_when_there_is_no_race():
    html = email_render.build_html(
        make_brief(), set(), dict(PLAN, days_to_race=None), THEME)
    assert "Days to Race" not in html
    assert "Goal" in html and "10k" in html


def test_zero_percent_sessions_adherence_still_prints():
    # `is not None`, not truthy — 0% of sessions is precisely the number that
    # most needs printing beside a flattering 83% total.
    html = email_render.build_html(
        make_brief(), set(), dict(PLAN, sessions_adherence_pct=0), THEME)
    assert "0% sessions" in html


def test_missing_sessions_adherence_prints_no_suffix():
    html = email_render.build_html(
        make_brief(), set(), dict(PLAN, sessions_adherence_pct=None), THEME)
    assert "sessions" not in html


@pytest.mark.parametrize("walk_mi,expected", [(6.0, "+6.0 walked"), (0, None), (None, None)])
def test_walk_miles_are_named_only_when_there_are_any(walk_mi, expected):
    html = email_render.build_html(
        make_brief(), set(), dict(PLAN, week_walk_mi=walk_mi), THEME)
    if expected:
        assert expected in html
    else:
        assert "walked" not in html


def test_today_callout_omitted_when_the_plan_has_no_workout_today():
    html = email_render.build_html(
        make_brief(), set(), dict(PLAN, today=None), THEME)
    assert "Yesterday you hit the session clean." not in html
    # The rest of the section survives.
    assert "Training Plan" in html and "83%" in html


def test_week_table_omitted_when_there_are_no_days():
    html = email_render.build_html(
        make_brief(), set(), dict(PLAN, last_7_days=[]), THEME)
    assert "Verdict" not in html
    assert "83%" in html


def test_missing_actual_miles_renders_an_em_dash_not_zero():
    # "0.0 mi actual" asserts a workout was done and measured at nothing;
    # a pending day has no actual at all.
    assert email_render._fmt_mi(None) == "—"
    assert email_render._fmt_mi(9.0) == "9.0 mi"


# --- _style_fragment -------------------------------------------------------

def test_style_fragment_styles_known_tags():
    out = email_render._style_fragment("<p>hi</p><table><td>x</td></table>", THEME)
    assert '<p style="' in out
    assert '<table style="' in out
    assert '<td style="' in out


def test_style_fragment_leaves_an_already_styled_tag_alone():
    out = email_render._style_fragment('<p style="color:red">hi</p>', THEME)
    assert out == '<p style="color:red">hi</p>'


def test_style_fragment_leaves_unknown_tags_untouched():
    # <strong>/<em> have sane client defaults; inventing rules fights the reader.
    out = email_render._style_fragment("<strong>hi</strong>", THEME)
    assert out == "<strong>hi</strong>"


def test_style_fragment_preserves_existing_attributes():
    out = email_render._style_fragment('<td colspan="2">x</td>', THEME)
    assert 'colspan="2"' in out
    assert "style=" in out


def test_style_fragment_does_not_touch_closing_tags():
    out = email_render._style_fragment("<p>hi</p>", THEME)
    assert out.endswith("</p>")


# --- plaintext alternative -------------------------------------------------

def test_text_alternative_carries_every_headline_and_the_details():
    brief = make_brief()
    text = email_render.build_text(brief, PLAN)
    for tk in brief.takeaways:
        assert tk.headline.upper() in text
        assert tk.summary in text
    assert "Plan: 9mi easy-steady." in text


def test_text_alternative_contains_no_html():
    text = email_render.build_text(make_brief(), PLAN)
    assert "<" not in text.replace("<=", "")


def test_text_alternative_includes_the_plan_when_present():
    text = email_render.build_text(make_brief(), PLAN)
    assert "TRAINING PLAN" in text
    assert "83% adherence · 83% sessions" in text
    assert "42 days to race" in text
    assert "14.2 mi / 36.0 mi" in text
    assert "MISSED" in text.upper()


def test_text_alternative_omits_the_plan_when_absent():
    text = email_render.build_text(make_brief(), None)
    assert "TRAINING PLAN" not in text
    # Takeaways still render — the plan is a section, not the message.
    assert "LONG RUN 9MI TODAY" in text


def test_text_alternative_is_never_empty():
    # A message with no text/plain part scores as more spam-like, and it is
    # what a watch or a screen reader is handed.
    assert email_render.build_text(make_brief(), None).strip()
