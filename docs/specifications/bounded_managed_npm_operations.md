# Bounded Managed NPM Operations

> **Status: superseded** by
> `native_runtime_distribution_and_verification.md`.
> claude-wrangler no longer runs npm. Install, upgrade, update, and
> repair fetch Claude Code's signed native binary with bounded,
> verified HTTPS downloads (requirement 8 there, with
> `--download-timeout-seconds`), and npm-era projects move to the
> native runtime through `legacy_npm_layout_migration.md`. The retired
> `--npm-timeout-seconds` and `--npm-install-loglevel` flags are still
> accepted so old scripts keep working, but they only print a warning.
> The text below is kept for history; do not implement against it.

## Purpose

Ensure managed npm operations fail with actionable diagnostics instead
of waiting indefinitely when registry access, package scripts, or npm's
optional network checks stall.

## Scope

This specification covers npm subprocesses started by `claude-wrangler`
during install, update, upgrade, and self-test operations.

## Requirements

1. Bounded subprocesses
   Every managed npm subprocess must run through a timeout-aware
   helper. When a timeout is exceeded, `claude-wrangler` must raise a
   `ClaudeWranglerError` that names the command and timeout duration.
2. Install defaults
   Managed package installation must avoid npm's nonessential
   install-time network checks by disabling audit, funding,
   update-notifier, and spinner progress behavior. This must not
   disable package scripts or otherwise change the installed Claude
   Code package payload.
3. Operator visibility
   Long-running npm operations must print the effective timeout before
   they start, using the existing stderr status channel. Managed
   package installation must emit useful troubleshooting progress by
   default without relying on npm's ambiguous spinner. HTTP fetch logs
   and foreground lifecycle-script output satisfy this requirement.
4. Override
   Operators must be able to adjust the npm timeout and managed install
   npm log level without editing source. Invalid timeout values and
   unsupported log-level values must fail during argument normalization
   with clear errors.
5. Repair cleanup
   Repair mode must remove npm's managed cache temp directory under the
   project-local cache, because interrupted installs can leave
   truncated tarballs there and block a clean retry.
6. Existing ownership boundaries
   The timeout and npm-configuration behavior must remain limited to
   managed `claude-wrangler` subprocesses and must not rewrite target
   project npm configuration files.

## Validation

- Unit tests must cover timeout failure reporting from the shared
  runtime helper.
- Unit tests must cover the install command arguments, npm environment,
  and timeout value used for managed package installation.
- Unit tests must cover rejecting invalid timeout configuration values.
- Unit tests must cover the default managed install log level and
  override.
- Unit tests must cover repair cleanup for npm cache temp files.
