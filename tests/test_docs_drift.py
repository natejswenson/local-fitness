"""Guards that keep `docs/mcp/` honest about the tool registries.

MCP is the ONLY client surface — there is no UI — so these pages are the whole
discovery surface for the app. A tool with no page is a tool nobody outside
this repo can find, and a page for a tool that no longer exists is worse than
no page at all.

Prose drifts silently because nothing executes it. The counts printed in
`README.md` and `docs/mcp/README.md` said 37 stdio / 35 HTTP while the
registries had grown to 45 / 43, and eight tools (the coach-memory quartet, the
two personality tools, the two report-card query tools) shipped with no page
while `docs/mcp/README.md` claimed "every tool, one page each". Nobody noticed
because no test read the docs. These do.

Each check pins the *actual claim sentence*, not a loose number match, so
editing the wording is a deliberate act that shows up as a failing test rather
than a stale number that keeps passing.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from local_fitness.agent import tools

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_MCP = REPO_ROOT / "docs" / "mcp"
ROOT_README = REPO_ROOT / "README.md"
MCP_README = DOCS_MCP / "README.md"

#: The two Availability phrasings the pages actually use. Pinned verbatim: the
#: line is a reader's one-glance answer to "can I call this from my phone", so
#: a page inventing a third phrasing is drift even when it means the same.
STDIO_AND_HTTP = "**Availability:** stdio + HTTP"
STDIO_ONLY = "**Availability:** stdio only — local"

ALL_TOOL_NAMES = frozenset(t.name for t in tools.ALL_TOOLS)
LOCAL_ONLY_NAMES = frozenset(t.name for t in tools.LOCAL_ONLY_TOOLS)
EVERY_TOOL_NAME = ALL_TOOL_NAMES | LOCAL_ONLY_NAMES


def _page(name: str) -> Path:
    return DOCS_MCP / f"{name}.md"


def _pages_on_disk() -> set[str]:
    return {p.stem for p in DOCS_MCP.glob("*.md") if p.name != "README.md"}


def test_registries_are_disjoint_and_non_empty():
    """The premise the rest of the module rests on: two registries, no overlap.

    A tool in both would be documented as stdio-only while actually being
    reachable over HTTP — the one drift these tests could not otherwise see.
    """
    assert ALL_TOOL_NAMES, "ALL_TOOLS is empty"
    assert LOCAL_ONLY_NAMES, "LOCAL_ONLY_TOOLS is empty"
    assert not (ALL_TOOL_NAMES & LOCAL_ONLY_NAMES), (
        "a tool is registered in BOTH ALL_TOOLS and LOCAL_ONLY_TOOLS: "
        f"{sorted(ALL_TOOL_NAMES & LOCAL_ONLY_NAMES)}"
    )


@pytest.mark.parametrize("name", sorted(EVERY_TOOL_NAME))
def test_every_registered_tool_has_a_page(name):
    path = _page(name)
    assert path.is_file(), (
        f"tool '{name}' is registered but has no docs/mcp/{name}.md — "
        "MCP is the only client surface, so an undocumented tool is "
        "undiscoverable"
    )
    assert path.read_text(encoding="utf-8").startswith(f"# `{name}`\n"), (
        f"docs/mcp/{name}.md must open with the H1 '# `{name}`'"
    )


def test_no_orphan_pages():
    """A page for a tool that no longer exists is actively misleading."""
    orphans = sorted(_pages_on_disk() - EVERY_TOOL_NAME)
    assert not orphans, (
        f"docs/mcp/ has pages for unregistered tools: {orphans} — delete the "
        "page, or re-register the tool"
    )


@pytest.mark.parametrize("name", sorted(ALL_TOOL_NAMES))
def test_all_tools_pages_claim_stdio_and_http(name):
    text = _page(name).read_text(encoding="utf-8")
    assert STDIO_AND_HTTP in text, (
        f"'{name}' is in ALL_TOOLS (reachable over stdio AND the networked "
        f"/mcp/ transport) so docs/mcp/{name}.md must carry the line "
        f"{STDIO_AND_HTTP!r}"
    )
    assert STDIO_ONLY not in text, (
        f"docs/mcp/{name}.md claims stdio-only, but '{name}' is in ALL_TOOLS"
    )


@pytest.mark.parametrize("name", sorted(LOCAL_ONLY_NAMES))
def test_local_only_pages_claim_stdio_only(name):
    text = _page(name).read_text(encoding="utf-8")
    assert STDIO_ONLY in text, (
        f"'{name}' is in LOCAL_ONLY_TOOLS (registered only by run_stdio(), "
        f"structurally unreachable over /mcp/) so docs/mcp/{name}.md must "
        f"carry the line {STDIO_ONLY!r}"
    )
    assert STDIO_AND_HTTP not in text, (
        f"docs/mcp/{name}.md claims HTTP reachability, but '{name}' is "
        "stdio-only"
    )


# --- the printed counts -----------------------------------------------------
#
# Anchored on the wording of each claim, not on "some number nearby": the
# regex must fail when the sentence is rewritten, so a reworded claim gets a
# fresh look instead of silently escaping the guard.

STDIO_COUNT = len(EVERY_TOOL_NAME)      # what `fitness mcp-stdio` serves
HTTP_COUNT = len(ALL_TOOL_NAMES)        # what the /mcp/ transport serves

_ROOT_README_CLAIM = re.compile(
    r"Once connected you get \*\*(\d+) tools over stdio\*\*\s+"
    r"\((\d+) over HTTP"
)
_STDIO_CONNECT_CLAIM = re.compile(
    r"\*\*Local, over stdio\*\* — all (\d+) tools, no token"
)
_HTTP_CONNECT_CLAIM = re.compile(
    r"\*\*Over the running server\*\* — (\d+) tools, bearer-gated"
)
_COUNT_TABLE_ROW = re.compile(
    r"\| Tool count \| \*\*(\d+)\*\* \| \*\*(\d+)\*\* \|"
)


def _one_match(pattern: re.Pattern, path: Path) -> re.Match:
    matches = list(pattern.finditer(path.read_text(encoding="utf-8")))
    assert len(matches) == 1, (
        f"expected exactly one {pattern.pattern!r} claim in {path.name}, "
        f"found {len(matches)} — the sentence was reworded or duplicated, so "
        "the drift guard is no longer reading the real claim"
    )
    return matches[0]


def test_root_readme_tool_counts_match_the_registries():
    m = _one_match(_ROOT_README_CLAIM, ROOT_README)
    assert int(m.group(1)) == STDIO_COUNT, (
        f"README.md says {m.group(1)} tools over stdio; ALL_TOOLS + "
        f"LOCAL_ONLY_TOOLS is {STDIO_COUNT}"
    )
    assert int(m.group(2)) == HTTP_COUNT, (
        f"README.md says {m.group(2)} over HTTP; ALL_TOOLS is {HTTP_COUNT}"
    )


def test_mcp_readme_connect_snippets_match_the_registries():
    stdio = _one_match(_STDIO_CONNECT_CLAIM, MCP_README)
    http = _one_match(_HTTP_CONNECT_CLAIM, MCP_README)
    assert int(stdio.group(1)) == STDIO_COUNT, (
        f"docs/mcp/README.md's stdio connect snippet says {stdio.group(1)}; "
        f"the stdio surface is {STDIO_COUNT}"
    )
    assert int(http.group(1)) == HTTP_COUNT, (
        f"docs/mcp/README.md's HTTP connect snippet says {http.group(1)}; "
        f"the /mcp/ surface is {HTTP_COUNT}"
    )


def test_mcp_readme_availability_table_matches_the_registries():
    m = _one_match(_COUNT_TABLE_ROW, MCP_README)
    assert (int(m.group(1)), int(m.group(2))) == (STDIO_COUNT, HTTP_COUNT), (
        f"docs/mcp/README.md's availability table says "
        f"{m.group(1)}/{m.group(2)}; the registries are "
        f"{STDIO_COUNT}/{HTTP_COUNT}"
    )


def test_mcp_readme_links_every_tool_page():
    """The index is the entry point — a page it never links is unreachable."""
    text = MCP_README.read_text(encoding="utf-8")
    unlinked = sorted(
        name for name in EVERY_TOOL_NAME if f"]({name}.md)" not in text
    )
    assert not unlinked, (
        f"docs/mcp/README.md has no link to these tool pages: {unlinked} — "
        "add a row to the matching 'Tools by area' table"
    )


# --- intra-docs links -------------------------------------------------------
#
# The 0.48.0 tool removal deleted docs/mcp/get_today_status.md, and three pages
# kept linking to it for seven releases — one of them (propose_training_plan)
# INSTRUCTING the reader to call the removed tool. The per-tool checks above
# can't see that: they read each page in isolation, never the links between
# pages. This one resolves every relative link target to a real file.

_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _relative_link_targets(path: Path):
    """Yield (target, resolved_path) for every relative link on the page.

    Fenced code blocks are stripped first — a page quoting example markdown
    must not fail the gate for links that are illustrations, not references.
    External schemes and pure-fragment links have no file to resolve.
    """
    text = _FENCED_CODE.sub("", path.read_text(encoding="utf-8"))
    for target in _MD_LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        bare = target.split("#", 1)[0]
        if not bare:
            continue
        yield target, (path.parent / bare).resolve()


def test_intra_docs_links_resolve():
    pages = sorted((REPO_ROOT / "docs").rglob("*.md")) + [ROOT_README]
    broken = [
        f"{page.relative_to(REPO_ROOT)} -> {target}"
        for page in pages
        for target, resolved in _relative_link_targets(page)
        if not resolved.exists()
    ]
    assert not broken, (
        "relative links pointing at files that don't exist:\n  "
        + "\n  ".join(broken)
        + "\nfix the link (or delete it with the page it pointed at)"
    )
