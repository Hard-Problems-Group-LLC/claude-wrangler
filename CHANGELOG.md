# Changelog

All notable changes to this repository will be documented in this
file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this repository aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
where practical.

## [Unreleased]

### Added

- Initial spirit-of-the-law port of codex-wrangler to manage
  project-local `@anthropic-ai/claude-code` installs, with channels
  mapped one-to-one onto the `stable`, `latest`, and `next` npm
  dist-tags.
- Generated `bin/claude-local` launcher that pins `CLAUDE_CONFIG_DIR`
  to `.local/claude` at every security ring, redirects `HOME` to
  `.local/claude/HOME` by default (ring 1), and supports per-invocation
  ring-0 escape via `--shared-home` or `CLAUDE_LOCAL_SHARED_HOME=1`.
- Four-level security ring model specification and AGENTS.md guidance
  (CPU-convention numbering; ring 3 sandbox default, ring 0 real HOME
  last resort), intended to be shared with codex-wrangler.
- Managed reasonable-permissions launcher default mapped to
  `--permission-mode acceptEdits`, shipped disabled.
- Managed `.gitignore` block covering `.local/`, `bin/claude-local`,
  and `README-LOCAL-Start-Claude.md` while never ignoring the
  trackable project-root `.claude/` directory or touching ignore
  blocks owned by TheKnowledge or codex-wrangler.
- Installer trust boundary that recognizes both `.local/claude/HOME`
  and a coexisting `.codex-home` as repo-local isolated homes.
- TheKnowledge submodule, starter bootstrap files, project-management
  trees, behavior specifications, and an open ECR requesting
  claude-wrangler awareness in TheKnowledge's managed ignore baseline.
- Foundation for a Node-free native Claude Code runtime:
  standard-library verification of both Anthropic manifest signatures
  against pinned trust anchors (`signing.py`), hardware-accurate
  platform selection and executable structure checks (`platforms.py`),
  HTTPS-only bounded downloads with resume, host allowlisting, and
  corporate CA support (`download.py`, `distribution.py`), an A/B
  runtime slot store with maintenance locking, atomic promotion, and
  in-use detection (`slots.py`, `atomicio.py`), and a standalone
  cross-platform launcher core (`launcher_core.py`). Recorded release
  manifests and signatures serve as offline test fixtures.
- Native Windows entry point `install.cmd` with a policy helper
  (`scripts/install-stage-1-windows.py`), and a project-owned
  `scripts/install_prerequisites.{py,sh,cmd}` family that installs only
  missing prerequisites through winget, brew, apt, or dnf, plus a
  `--dev` path for contributors on Windows.
- `scripts/run_tests_isolated.py`, which runs pytest with every
  home-like variable redirected, and an autouse test fixture that does
  the same per test.

### Changed
- On Windows, the user-level `claude-wrangler` launcher is a copied
  `claude-wrangler.exe` (renamed aside when in use) instead of a
  symlink, and installers print a PATH hint instead of failing.
- Repo-relative layout paths are stored in POSIX form on every OS;
  `.gitignore` block edits never produce `\r\r\n`; the user home
  resolves from `USERPROFILE` on Windows; codex-wrangler's current
  `.local/codex-home` is recognized as an isolated home.
- Managed installs now prune npm's orphaned ".<name>-<token>" rename-staging directories from the local node_modules tree before running `npm install`, so an interrupted or crashed install self-heals instead of failing every retry with "ENOTEMPTY: directory not empty, rename". Only npm scratch state is removed; installed packages and project-local Claude session state are never touched.


### Fixed

- The generated `bin/claude-local` launcher now exports
  `DISABLE_UPDATES=1`, `DISABLE_AUTOUPDATER=1`, and
  `DISABLE_INSTALLATION_CHECKS=1`. Claude Code's self-updater targets
  `HOME`-relative paths rather than the running binary, so a ring-0
  session could previously rewrite or delete the operator's global
  Claude Code install. Rerun `--update` to regenerate existing
  launchers.
