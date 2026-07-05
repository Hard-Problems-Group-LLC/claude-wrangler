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

### Changed
- Managed installs now prune npm's orphaned ".<name>-<token>" rename-staging directories from the local node_modules tree before running `npm install`, so an interrupted or crashed install self-heals instead of failing every retry with "ENOTEMPTY: directory not empty, rename". Only npm scratch state is removed; installed packages and project-local Claude session state are never touched.


### Fixed
