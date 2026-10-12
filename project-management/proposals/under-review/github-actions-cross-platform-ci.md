# Cross-Platform GitHub Actions CI

Title: Engineering Change Request: add GitHub Actions CI on Windows,
Linux, and macOS
Author: Claude (AI)
Date: 2026-10-12T01:05:00-07:00
Status: Under review (operator asked for a proposal only; no remote CI yet)
Reviewers: operator (repository maintainer)
Related Work: `windows-support-and-native-claude-runtime.md` (approved);
TheKnowledge `scripts/windows/README.txt` ("Windows GitHub Actions remains
the authoritative compatibility gate").

## Problem Statement

The repository has no CI. Windows support adds platform-specific code
paths (cmd shims, Windows home variables, file locking, PE checks) and the
macOS port will add more. Local runs on one operator machine cannot cover
all three operating systems, and agent-run consoles differ from human
consoles (for example, UTF-8 versus code page 437).

## Proposal

Add `.github/workflows/ci.yml` with:

- A `test` job on `windows-latest`, `ubuntu-latest`, and `macos-latest`
  with Python 3.12, running Black, Ruff, compileall, and pytest.
- A `bootstrap-floor` job on `ubuntu-latest` that runs the
  Python 3.9-constrained modules (stage 1/2 helpers, prerequisite
  installer, launcher core, verifier) under Python 3.9.
- A `windows-entry` job on `windows-latest` that runs `install.cmd` from
  cmd and PowerShell with `USERPROFILE`, `APPDATA`, and `LOCALAPPDATA`
  redirected to `%RUNNER_TEMP%\home`, then runs the installed
  `claude-wrangler.exe --help`.

Constraints:

- Pin every action by full commit SHA.
- `submodules: false`; TheKnowledge is reached over an SSH alias the
  runner does not have. Tests that need TheKnowledge must skip cleanly.
- No network access to `downloads.claude.ai` in unit tests; signature
  tests use recorded fixtures. An optional, manually triggered
  `live-release` job may fetch and verify the current `stable` manifest.
- Hosted runners run elevated on Windows; set
  `THEKNOWLEDGE_INSTALL_ALLOW_ELEVATED=1` only in the `windows-entry`
  job.

## Draft Workflow

```yaml
name: ci
on:
  push:
    branches: [trunk]
  pull_request:
permissions:
  contents: read
jobs:
  test:
    strategy:
      fail-fast: false
      matrix:
        os: [windows-latest, ubuntu-latest, macos-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@<sha>  # pin
        with:
          submodules: false
      - uses: actions/setup-python@<sha>  # pin
        with:
          python-version: "3.12"
      - run: python -m pip install -e ".[dev]"
      - run: python -m black --check src tests scripts
      - run: python -m ruff check src tests scripts
      - run: python -m compileall -q src scripts
      - run: python -m pytest -q
  bootstrap-floor:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<sha>
      - uses: actions/setup-python@<sha>
        with:
          python-version: "3.9"
      - run: python -m compileall -q scripts src/claude_wrangler/signing.py
```

## Open Questions

1. Whether to run the `live-release` job on a schedule (for example,
   daily) to detect upstream format changes (signature format, platform
   keys) early.
2. Whether macOS runners are worth their cost before the Mac port starts.
