# Cross-Platform GitHub Actions CI

Title: Engineering Change Request: add GitHub Actions CI on Windows,
Linux, and macOS
Author: Claude (AI)
Date: 2026-10-12T01:05:00-07:00
Revised: 2026-10-12T07:46:00-07:00 (backlog entry "Correct the
cross-platform CI proposal draft", review CI-1: full suite on Python 3.9
and 3.12, the isolated runner everywhere, musl and arm64 legs,
actionlint, and prose that matches the YAML)
Status: Under review (operator asked for a proposal only; no remote CI yet)
Reviewers: operator (repository maintainer)
Related Work: `windows-support-and-native-claude-runtime.md` (approved);
TheKnowledge `scripts/windows/README.txt` ("Windows GitHub Actions remains
the authoritative compatibility gate").

## Problem Statement

The repository has no CI. Windows support adds platform-specific code
paths (cmd shims, Windows home variables and ACLs, job objects, file
locking, PE checks), and the POSIX paths (`fcntl` locking, `/proc`, `ps`,
and `libproc` process listing, execute bits, directory fsync, mode 0700)
have not run outside Windows at all. The Python 3.9 floor applies to the
bootstrap, the launcher core, and the verifier, but has only been run
locally on Windows. Local runs on one operator machine cannot cover all
three operating systems, and agent-run consoles differ from human
consoles (for example, UTF-8 versus code page 437).

## Proposal

Add `.github/workflows/ci.yml` with five jobs. Every test run goes
through `python scripts/run_tests_isolated.py -q --timeout 180`, never
bare `pytest`, so every home-like variable points into a temporary
directory on every runner.

- `lint` (ubuntu-latest, Python 3.12): Black and Ruff in check mode,
  `compileall` of `src` and `scripts`, and actionlint over the workflow
  itself (installed with `go install` at a pinned version, which the Go
  checksum database verifies).
- `test`: the full suite on `ubuntu-latest`, `macos-latest` (Apple
  silicon), and `windows-latest`, each on Python 3.9 and Python 3.12.
  The 3.12 legs install the `[dev]` extras. The 3.9 legs install the
  package plus `pytest==8.4.2` and `pytest-timeout==2.4.0`, because the
  `[dev]` pins (pytest 9, Black 26) need Python 3.12; Python 3.9 mirrors
  the oldest interpreter the project supports (macOS ships 3.9.6 with
  the Command Line Tools). If a runner image has no 3.9 build for its
  architecture, that leg moves to `macos-13` (Intel) rather than being
  dropped.
- `test-musl`: the full suite on Python 3.12 inside a
  `python:3.12-alpine` container on `ubuntu-latest`, which exercises the
  musl platform detection and the POSIX paths on a second libc.
- `test-arm64`: the full suite on Python 3.12 on `ubuntu-24.04-arm`,
  where available (GitHub provides it for public repositories; private
  repositories need a larger runner). If the runner label is
  unavailable the job is skipped by a repository variable rather than
  failing the workflow.
- `windows-entry` (windows-latest): runs `install.cmd` from cmd and
  PowerShell with `USERPROFILE`, `APPDATA`, and `LOCALAPPDATA` redirected
  to `%RUNNER_TEMP%\home`, then runs the installed
  `claude-wrangler.exe --help` from that home.

Constraints:

- Pin every action by full commit SHA, with the release tag in a
  comment. The SHAs in the draft below were taken from memory while
  drafting and must be refreshed (and checked against the tagged
  release) when the workflow is adopted; Dependabot can keep them
  current afterwards. The `python:3.12-alpine` image should likewise be
  pinned by digest when adopted.
- `actions/checkout` runs with `submodules: false` and
  `persist-credentials: false`. TheKnowledge is reached over an SSH
  alias the runner does not have; tests that need it must skip cleanly.
- `permissions: contents: read` for the whole workflow.
- No network access to `downloads.claude.ai` in any test; signature tests
  use recorded fixtures. `CLAUDE_WRANGLER_TEST_REAL_BINARY` stays unset,
  so the opt-in real-binary test skips. An optional, manually triggered
  `live-release` job may fetch and verify the current `stable`
  manifest (open question 1).
- Hosted Windows runners run elevated; set
  `THEKNOWLEDGE_INSTALL_ALLOW_ELEVATED=1` only in the `windows-entry`
  job.
- The Windows shim tests compile their fake Claude Code with the .NET
  Framework `csc.exe`, which hosted Windows images include; they skip
  elsewhere.

## Draft Workflow

```yaml
name: ci
on:
  push:
    branches: [trunk]
  pull_request:
  workflow_dispatch:
permissions:
  contents: read
env:
  PYTHONUTF8: "1"
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      # SHAs below must be refreshed when this workflow is adopted.
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
        with:
          submodules: false
          persist-credentials: false
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065  # v5.6.0
        with:
          python-version: "3.12"
      - run: python -m pip install -e ".[dev]"
      - run: python -m black --check src tests scripts
      - run: python -m ruff check src tests scripts
      - run: python -m compileall -q src scripts
      - name: actionlint
        run: |
          go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.7
          "$(go env GOPATH)/bin/actionlint" -color

  test:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python: ["3.9", "3.12"]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
        with:
          submodules: false
          persist-credentials: false
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065  # v5.6.0
        with:
          python-version: ${{ matrix.python }}
      - name: Install (Python 3.12, dev extras)
        if: matrix.python == '3.12'
        run: python -m pip install -e ".[dev]"
      - name: Install (Python 3.9 floor)
        if: matrix.python == '3.9'
        run: |
          python -m pip install -e .
          python -m pip install pytest==8.4.2 pytest-timeout==2.4.0
      - name: Full suite through the isolated runner
        run: python scripts/run_tests_isolated.py -q --timeout 180

  test-musl:
    runs-on: ubuntu-latest
    container:
      image: python:3.12-alpine  # pin by digest when adopted
    steps:
      - run: apk add --no-cache git bash
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
        with:
          submodules: false
          persist-credentials: false
      - run: python -m pip install -e ".[dev]"
      - run: python scripts/run_tests_isolated.py -q --timeout 180

  test-arm64:
    if: vars.CI_ARM64_RUNNER_AVAILABLE == 'true'
    runs-on: ubuntu-24.04-arm
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
        with:
          submodules: false
          persist-credentials: false
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065  # v5.6.0
        with:
          python-version: "3.12"
      - run: python -m pip install -e ".[dev]"
      - run: python scripts/run_tests_isolated.py -q --timeout 180

  windows-entry:
    runs-on: windows-latest
    env:
      THEKNOWLEDGE_INSTALL_ALLOW_ELEVATED: "1"
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
        with:
          submodules: false
          persist-credentials: false
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065  # v5.6.0
        with:
          python-version: "3.12"
      - name: install.cmd from cmd with a redirected profile
        shell: cmd
        run: |
          set "USERPROFILE=%RUNNER_TEMP%\home"
          set "APPDATA=%RUNNER_TEMP%\home\AppData\Roaming"
          set "LOCALAPPDATA=%RUNNER_TEMP%\home\AppData\Local"
          mkdir "%APPDATA%" "%LOCALAPPDATA%"
          call install.cmd --skip-submodule-init
          if errorlevel 1 exit /b 1
          "%USERPROFILE%\.local\bin\claude-wrangler.exe" --help
      - name: install.cmd from PowerShell with a redirected profile
        shell: pwsh
        run: |
          $env:USERPROFILE = "$env:RUNNER_TEMP\home-ps"
          $env:APPDATA = "$env:USERPROFILE\AppData\Roaming"
          $env:LOCALAPPDATA = "$env:USERPROFILE\AppData\Local"
          New-Item -ItemType Directory -Force $env:APPDATA, $env:LOCALAPPDATA | Out-Null
          & .\install.cmd --skip-submodule-init
          if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
          & "$env:USERPROFILE\.local\bin\claude-wrangler.exe" --help
          exit $LASTEXITCODE
```

## Open Questions

1. Whether to run a manually triggered or scheduled `live-release` job
   (for example, daily) that fetches and verifies the current `stable`
   manifest, to detect upstream format changes (signature format,
   platform keys) early. It would be the only job with network access to
   the release host.
2. Whether the macOS legs are worth their runner cost before the Mac
   port validation in the backlog starts; they are included because the
   POSIX code paths have never run.
3. Whether to add the bash 3.2 `install.sh` check from the open
   TheKnowledge ECR (`ECRs/TheKnowledge/open/
   install-sh-bash32-empty-array-and-cdpath.md`) as a macOS step once
   the upstream fix lands.
