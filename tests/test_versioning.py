"""Tests for single-source package versioning and packaging metadata."""

from pathlib import Path

from claude_wrangler import __version__
from claude_wrangler.constants import SCRIPT_VERSION


def test_package_and_script_versions_match():
    assert SCRIPT_VERSION == __version__


def test_pyproject_uses_dynamic_package_version():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in pyproject
    assert 'version = {attr = "claude_wrangler.__version__"}' in pyproject


def test_pyproject_declares_project_urls():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert "[project.urls]" in pyproject
    assert "Repository =" in pyproject
    assert "Issues =" in pyproject


def test_dev_extra_matches_pinned_requirements_file():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    requirements = [
        line.strip()
        for line in Path("requirements-dev.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.startswith("#")
    ]
    for requirement in requirements:
        assert '"{}"'.format(requirement) in pyproject
