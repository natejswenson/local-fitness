"""Tests for agent/branding.py — the PRESS default theme + the
LOCAL_FITNESS_BRAND_FILE local-override contract."""
from __future__ import annotations

import json

from local_fitness.agent import branding


def test_default_theme_is_press(monkeypatch):
    monkeypatch.delenv("LOCAL_FITNESS_BRAND_FILE", raising=False)
    t = branding.load_theme()
    assert t["name"] == "press"
    assert t["colors"]["paper"] == "#F5F0E6"
    assert t["colors"]["ink"] == "#181510"
    assert t["colors"]["dim"] == "#6E675C"
    assert t["colors"]["accent"] == "#E8501F"
    assert t["identity"]["stamp"] == "NS"
    assert t["fonts"]["mono_file"] is None


def test_brand_file_deep_merges_over_default(monkeypatch, tmp_path):
    f = tmp_path / "brand.json"
    f.write_text(json.dumps({
        "colors": {"accent": "#0055FF"},
        "identity": {"stamp": "XY"},
    }), encoding="utf-8")
    monkeypatch.setenv("LOCAL_FITNESS_BRAND_FILE", str(f))
    t = branding.load_theme()
    # Overridden keys take, un-named siblings keep their defaults.
    assert t["colors"]["accent"] == "#0055FF"
    assert t["colors"]["paper"] == "#F5F0E6"
    assert t["identity"]["stamp"] == "XY"
    assert t["identity"]["byline"] == "linkedin.com/in/natejswenson"


def test_missing_brand_file_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_FITNESS_BRAND_FILE", str(tmp_path / "nope.json"))
    t = branding.load_theme()
    assert t["colors"]["paper"] == "#F5F0E6"


def test_corrupt_brand_file_falls_back_to_default(monkeypatch, tmp_path):
    f = tmp_path / "brand.json"
    f.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("LOCAL_FITNESS_BRAND_FILE", str(f))
    t = branding.load_theme()
    assert t["colors"]["ink"] == "#181510"


def test_non_object_brand_file_falls_back(monkeypatch, tmp_path):
    f = tmp_path / "brand.json"
    f.write_text('["not", "a", "dict"]', encoding="utf-8")
    monkeypatch.setenv("LOCAL_FITNESS_BRAND_FILE", str(f))
    assert branding.load_theme()["colors"]["accent"] == "#E8501F"


def test_mono_file_tilde_expansion(monkeypatch, tmp_path):
    f = tmp_path / "brand.json"
    f.write_text(json.dumps({"fonts": {"mono_file": "~/somewhere/font.ttf"}}),
                 encoding="utf-8")
    monkeypatch.setenv("LOCAL_FITNESS_BRAND_FILE", str(f))
    mono_file = branding.load_theme()["fonts"]["mono_file"]
    assert "~" not in mono_file
    assert mono_file.endswith("/somewhere/font.ttf")


def test_load_theme_returns_fresh_copies(monkeypatch):
    # Mutating a returned theme must never bleed into DEFAULT_THEME.
    monkeypatch.delenv("LOCAL_FITNESS_BRAND_FILE", raising=False)
    t = branding.load_theme()
    t["colors"]["ink"] = "#FF0000"
    assert branding.DEFAULT_THEME["colors"]["ink"] == "#181510"
    assert branding.load_theme()["colors"]["ink"] == "#181510"
