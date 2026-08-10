"""Local fitness agent — Garmin data → SQLite → Claude Agent SDK briefings."""

import importlib.metadata

# Same rule as tools.server_version(): read installed package metadata, never
# a literal. The literal here sat at "0.4.0" while pyproject reached 0.55.0 —
# 51 minor versions stale — because nothing imported it and nothing tested it.
# The fallback mirrors server_version(): a source checkout running without an
# install must never fail on a version string.
try:
    __version__ = importlib.metadata.version("local-fitness")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"
