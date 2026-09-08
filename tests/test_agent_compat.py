"""Keep the checked-in Claude and Codex entry points on one contract."""
from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_codex_bootstrap_points_to_canonical_claude_guidance():
    bootstrap = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "`CLAUDE.md` is the canonical repository guidance" in bootstrap
    assert "Read `CLAUDE.md` completely" in bootstrap


def test_codex_project_config_runs_the_public_stdio_entrypoint():
    with (REPO_ROOT / ".codex" / "config.toml").open("rb") as config_file:
        config = tomllib.load(config_file)

    fitness = config["mcp_servers"]["fitness"]
    assert fitness["command"] == "uv"
    assert fitness["args"] == ["run", "fitness", "mcp-stdio"]
    assert "env" not in fitness, "project config must not contain credentials"
