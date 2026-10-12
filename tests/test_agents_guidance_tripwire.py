"""Tripwires for project guidance that specifications require in AGENTS.md.

`prerequisites_and_platform_entry_points.md` requirement 7 needs the warning
against refreshing TheKnowledge from a CRLF Windows checkout, and the
`security_ring_model.md` validation section needs the ring definitions,
escalation policy, and recorded ring requirements. A TheKnowledge starter
refresh rewrites AGENTS.md, so these tests catch a refresh that drops the
project-specific sections.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

AGENTS_PATH = Path(__file__).resolve().parent.parent / "AGENTS.md"


@pytest.fixture(scope="module")
def agents_text() -> str:
    """Return AGENTS.md with whitespace runs folded to single spaces."""

    return re.sub(r"\s+", " ", AGENTS_PATH.read_text(encoding="utf-8"))


def test_agents_warns_against_crlf_theknowledge_refreshes(agents_text: str) -> None:
    assert "core.autocrlf=true" in agents_text
    assert "TheKnowledge/scripts/update_theknowledge_submodule.py" in agents_text
    assert "initial-setup --force" in agents_text
    assert "Refresh TheKnowledge from a Linux or WSL2 checkout" in agents_text


def test_agents_documents_the_security_rings(agents_text: str) -> None:
    assert "## Security Ring Model" in agents_text
    assert "docs/specifications/security_ring_model.md" in agents_text
    for ring in (
        "Ring 3 (default)",
        "Ring 2:",
        "Ring 1:",
        "Ring 0 (last resort)",
    ):
        assert ring in agents_text, ring
    assert "claude-local --shared-home" in agents_text
    assert "CLAUDE_LOCAL_SHARED_HOME=1" in agents_text


def test_agents_documents_the_escalation_policy(agents_text: str) -> None:
    assert "Escalate toward ring 0 only after" in agents_text
    assert "`CLAUDE_CONFIG_DIR` stays pinned to `.local/claude` at every ring" in (
        agents_text
    )
    assert "requires explicit operator confirmation" in agents_text
    assert "### Recorded Ring Requirements" in agents_text
    assert "requires ring <N> because <reason> (YYYY-MM-DD)" in agents_text
