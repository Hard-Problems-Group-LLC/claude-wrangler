<!-- THEKNOWLEDGE_MANAGED_HEADER_START -->
This project uses The Hard Problems Group's specifications and guidance
framework, TheKnowledge. Both AI agents and human developers should reference
`TheKnowledge/AGENTS.md` for detailed instructions.

AI agents must load `TheKnowledge/AGENTS.md` before running automated
tooling that edits, validates, stages, or tests repository files.

Keep only a very small amount of context locked in active memory: that
preflight rule, any live-state override record map, and repo-specific
non-destructive safety limits.

---

Any project-specific `AGENTS.md` content should go below this line and above
the managed TheKnowledge footer.
<!-- THEKNOWLEDGE_MANAGED_HEADER_END -->

## Project-Specific Instructions

- Maintain the top-level `CHANGELOG.md` using a Keep a Changelog style
  structure with an `Unreleased` section.
- Treat `project-management/completed-tasks.txt` as execution history for
  human and AI work, not as a substitute for the product changelog.
- Before commit workflows clear
  `project-management/state/pending-commit-changes.txt`, preserve its
  notable repository changes in `CHANGELOG.md`.
- Prefer `python scripts/git_commit_with_changelog.py -m "<subject>"` over
  a direct call to the TheKnowledge commit helper so the changelog sync
  happens before the pending-commit queue is truncated.
- This project is designed to coexist with `codex-wrangler` in the same
  consuming repository. Never let managed artifacts, `.gitignore` blocks,
  or installer guards collide with the Codex equivalents
  (`.codex-local/`, `.codex-home/`, `bin/codex-local`,
  `README-LOCAL-Start-Codex.md`).
- Treat `.local/claude/` (managed npm install, redirected HOME, pinned
  `CLAUDE_CONFIG_DIR` state), `bin/claude-local`, and
  `README-LOCAL-Start-Claude.md` as local operator tooling or generated
  artifacts when they exist. Do not classify
  `.local/claude/npm/package.json` as this repository's tracked
  dependency manifest.
- Do not ignore or delete the project-root `.claude/` directory by
  automation; Claude Code keeps trackable project settings there.
- On Windows checkouts with `core.autocrlf=true`, do not run
  `TheKnowledge/scripts/update_theknowledge_submodule.py` or
  `initial-setup --force`. Their forced starter refresh rewrites
  `.gitignore` with `\r\r\n` line endings, which stops ignoring
  `.codex-home/`, `.codex-local/`, and
  `.theknowledge-restricted-names.local`. Refresh TheKnowledge from a
  Linux or WSL2 checkout instead.
- Until the test suite is Windows-clean, the standard commit helper is
  unsafe on Windows: it always pushes, and its quality gate runs pytest
  against the real `USERPROFILE`. On Windows, run the checks with the
  home variables redirected and commit with plain `git` after the
  staging review; push only on explicit operator approval.

## Security Ring Model

This project defines four security rings for agent build, test, debug,
and operations work. Ring numbering follows the CPU protection-ring
convention: ring 0 is the most privileged, and privilege decreases as
the ring number increases. See
`docs/specifications/security_ring_model.md` for the full
specification. These definitions are shared guidance; `codex-wrangler`
is expected to adopt the same numbering at its next upgrade.

- Ring 3 (default): agent-managed sandbox with its own ephemeral,
  sandbox-internal `$HOME`.
- Ring 2: sandboxed, with `$HOME` redirected to the project-local
  persistent home `.local/claude/HOME`.
- Ring 1: outside the sandbox, `$HOME` still `.local/claude/HOME`. The
  generated `bin/claude-local` launcher starts sessions at this ring.
- Ring 0 (last resort): outside the sandbox with the operator's real
  `$HOME`. Reach it per-invocation with `claude-local --shared-home` or
  `CLAUDE_LOCAL_SHARED_HOME=1`.

Operating rules:

- Run build, test, and debug work at ring 3 whenever reasonable.
  Escalate toward ring 0 only after the current ring has been
  established as inadequate for the specific task at hand.
- `CLAUDE_CONFIG_DIR` stays pinned to `.local/claude` at every ring;
  escalation must never fork Claude auth or session history.
- When a task class is found to require a lower ring, record it in the
  ring requirements list below so future sessions do not rediscover the
  limitation.
- Changing this project's default ring always requires explicit
  operator confirmation, and the new default must be recorded here.

### Recorded Ring Requirements

Add entries as
`<task class>: requires ring <N> because <reason> (YYYY-MM-DD)`.

- Default ring on native Windows hosts (operator-confirmed default
  change): ring 1, because Claude Code has no sandbox on native Windows,
  so rings 3 and 2 do not exist there. Ring 1 on Windows redirects
  `HOME`, `USERPROFILE`, `HOMEDRIVE`/`HOMEPATH`, `APPDATA`,
  `LOCALAPPDATA`, and `XDG_*`; the launcher warns each session, with a
  per-project opt-out for personal work. Use WSL2 or a separate Windows
  account where client work needs ring 3 (2026-10-12).
- Native-Windows install and entry-point tests: run at ring 1 with
  `HOME`, `USERPROFILE`, `APPDATA`, and `LOCALAPPDATA` redirected into
  temporary directories, because some tests invoke installers that
  resolve the user home from `USERPROFILE`. A real-profile install is
  ring 0 and is operator-run only (2026-10-12).
- Claude Code housekeeping at ring 0: requires accepting that a ring-0
  session may prune old versions in the operator's global
  `~/.local/share/claude/versions`, because Claude Code's background
  cleanup ignores the update kill switches; prefer ring 1 (2026-10-12).

<!-- THEKNOWLEDGE_MANAGED_FOOTER_START -->
---

<!-- TheKnowledge-managed footer: place overrides here when the consuming
project needs behavior different from TheKnowledge's own repository setup. -->

## TheKnowledge Overrides
- Use `project-management/backlog.txt` as ordered pending work.
- Keep `project-management/tasks-in-progress.txt` minimal and current.
- Record finished work at the top of
  `project-management/completed-tasks.txt` with ISO 8601 timestamps.
- Track operator actions for AI in `project-management/ai-human-requests.txt`.
- Track proposal records as Markdown files under
  `project-management/proposals/` using `approved/`, `rejected/`,
  `deferred/`, and `under-review/`.
- Use `project-management/deferred.txt` for explicitly deferred work.
- Queue brief commit-ready summaries in
  `project-management/state/pending-commit-changes.txt`.
- Treat `pending-commit-changes.txt` as short-lived commit-body input, not
  durable release history. Projects with durable user-facing,
  operator-facing, or release-facing history needs should keep a
  `CHANGELOG.md` and copy or summarize notable pending-queue entries there
  before the queue is cleared by a successful commit.
- Maintain bug lifecycle summary files under `project-management/bugs/` and
  detailed bug records under `open/`, `in-progress/`, and `closed/`.
- Use `TheKnowledge/standards-and-practices/docs/`
  `AI-backlog-iteration.txt` when told to iterate the backlog.
- Use the consuming project's own `project-management/git-flow.txt` for branch
  and merge operations.
- Treat source documentation as delivery work. Every maintained source file
  should carry top-of-file context, every type and callable should be
  documented, and non-trivial control flow should carry local rationale where
  structure alone would be ambiguous.
- Prefer Markdown (`.md`) for maintained prose documents, internal records,
  starter documents, and generated guidance unless a file is machine-consumed,
  intentionally extensionless, or temporarily kept in a legacy format during
  an explicit migration.
- Prefer patch-style edits for small, localized manual changes. For
  whole-file records, generated content, mechanical multi-file updates, or
  environments where patch helpers are unavailable or failing, use explicit
  named-file edits or short scripts with reviewable scope. Always inspect the
  resulting diff, preserve unrelated user work, and record repeated tool
  failures as bugs or local reliability notes instead of retrying them
  indefinitely.
- Treat project-root `.local/` as the standard home for checkout-local
  operator state and local policy inputs that AI agents must consider but
  must not commit or push.
- Load `.local/ai-local-notes.md` or `.local/ai-local-notes.txt` before broad
  automation, validation, staging, or cleanup when either file exists.
- Treat those local note files plus `.theknowledge-restricted-names.local` as
  local-only policy inputs: obey them when present, keep them untracked, and
  do not copy their contents into tracked prose unless the operator explicitly
  directs that.
- Do not name external client projects in TheKnowledge-bound records,
  proposals, bugs, feedback, or generated guidance. Use `an external project`
  or another operator-approved generic phrase instead.
- Keep any restricted-name source list local and untracked, such as
  `.theknowledge-restricted-names.local` or an out-of-tree equivalent, and do
  not flag the list itself during validation.
- If a restricted name appears as a directory or filename component, stop and
  ask the operator how to resolve the path before renaming, deleting,
  redacting, or moving anything.
- Imported ECR holding directories must use project-neutral names such as
  `imported-ecrs/`, not source-project names. Keep imported ECR basenames
  unchanged as source-facing identifiers, and track accepted or implemented
  files in the active TheKnowledge `accepted-ecrs-list.md`.
- Bootstrap, setup, prerequisite, and environment-selection code should
  follow Python 3.9 best practices unless a higher floor is documented.
- Normal runtime, automation, test, and developer-tooling code should follow
  Python 3.12 best practices unless a component is intentionally constrained.
- Use `./install.sh` when the project is relying on the managed starter
  Python toolchain. The default starter installs `install.sh`,
  `bootstrap.sh`, `scripts/install-stage-2.py`, `bootstrap-stage2.py`,
  `python-environments.json`, `.python-version`, `set-context.sh`,
  `set-context-bootstrap.sh`, `requirements-dev.txt`,
  `scripts/dev_setup.py`, `scripts/python_environment_bootstrap.py`,
  `scripts/tool_validation_profiles.py`, `tool_execution_constraints.json`,
  `tool_validation_profiles.json`, and a managed `.gitignore` block. That
  flow starts from Python 3.9+, defaults to a user-local standard install,
  supports explicit repo-local development mode and venv-only mode, permits
  system installs only with `--system` under root, requires `direnv` for
  development mode, and proactively ignores `.local/`,
  `.theknowledge-restricted-names.local`, `.codex-local/`, `.codex-home/`,
  `.codex`, `bin/codex-local`, and `README-LOCAL-Start-Codex.md`.
  `bootstrap.sh` and `bootstrap-stage2.py` remain compatibility wrappers.
- Treat those Codex and codex-wrangler paths as local operator tooling or
  generated artifacts. Do not classify `.codex-local/package.json` as the
  consuming repository's tracked dependency manifest, and extend the managed
  ignore baseline deliberately when future local Codex artifact paths appear.
- For substantive development work, prefix intermediary status
  updates with an inline bracketed ISO 8601 timestamp including the
  timezone offset, for example
  `[2026-03-25T01:05:12-07:00] Running full pytest.`
- Use timestamped updates when work begins, before and after
  commands or waits likely to take more than a few seconds, and at
  major phase boundaries.
- Include elapsed durations when they are easy to compute.
- Keep final answers readable; this rule applies to intermediary
  development updates for workflow profiling, not to every sentence
  of casual chat.
- Treat the literal phrase `collision resume` as a session-continuity
  directive for the current workspace. Do not assume the newest SSH, `mosh`,
  terminal, or Codex session is correct; inspect candidate sessions when
  session data is available.
- For `collision resume`, prefer the prior session whose workspace, age,
  origin, and current activity best match the task. If ambiguity remains,
  summarize the candidates by origin, age, workspace, and current activity
  before acting.
- Preserve both the phrase `collision resume` and its operational meaning in
  handoffs, compactions, and summaries.
- Before mutating outside the active project root, stop and ask for explicit
  cross-project confirmation in the current session. Name the active project
  or path, the target project or path, and the intended change class.
- Cross-project mutations include file edits, deletes, generated files,
  project-management records, service restarts, container rebuilds, queued
  jobs, commits, pushes, and deployments. Read-only inspection may proceed
  when needed to identify the owning project, repository, or service.
- Do not infer cross-project authorization from a URL, adjacent discussion,
  command-escalation approval, or a prior session. When practical, disclose
  any detected active writer in the target project before proceeding.
- Before any `git add`, list the files about to be staged and ask the
  operator whether to review them.
- Offer these staging-review choices: `1.` review at least one file in the
  changeset, `2.` proceed without review for this changeset, `3.` proceed and
  suppress review prompts for the rest of the current session until the
  operator asks to resume them.
- If review is requested, prefer changeset review in Meld when
  available as the default visual review path.
- Launch `meld .` from the repository or submodule root so Meld opens its
  version-control view for the full working tree.
- If Meld version-control view is unavailable or unsuitable, compare a
  temporary clean snapshot directory against the working tree in Meld's
  folder-comparison mode.
- Otherwise offer file-by-file review in the conversation or abort the
  staging step.
- Never guess a Git author or committer email address from commit history,
  hostnames, remote URLs, network overlays, or similar context.
- Require an explicit commit identity before creating a commit. Prefer
  configured `git` properties such as `user.name` and `user.email`; explicit
  `GIT_COMMITTER_*` and optional separate `GIT_AUTHOR_*` overrides are also
  acceptable when set deliberately.
- When a separate author identity is not explicitly configured, reuse the
  explicit committer identity for author instead of inventing a second
  address.
- Run `git diff` before any `git add` and `git diff --cached` before any
  commit. The standardized commit helper does both automatically.
- Use `python TheKnowledge/scripts/git_standard_commit_push.py -m
  "<subject>"` after the operator has either reviewed the changes or
  explicitly approved proceeding. Pass `--assume-reviewed` only after an
  explicit review decision made outside the helper. Use
  `--resume-review-prompts` to re-enable prompts for the current shell
  session. The helper rejects commits whose author/committer identity is not
  explicitly configured.
- `ACP` means "add, commit, push" through the project's VCS workflow. Treat
  it as a request to stage, commit, and push meaningful blocks with
  appropriate comments; it is not permission to collapse unrelated work into
  one monolithic commit or skip review, validation, and identity rules.
- When working primarily in the consuming project and discovering bugs,
  proposals, complaints, or general notes about TheKnowledge itself,
  record them on the TheKnowledge `Feedback` branch.
- When filing that feedback from a consuming project, use the active
  `TheKnowledge/` submodule checkout in that project. Prefer
  `python TheKnowledge/scripts/send_theknowledge_feedback.py prepare`
  and `finish` so the helper captures and restores the submodule state for
  you. Use `--push` only when the configured remote push URL is writable for
  the current operator, and use `abort` when you want to restore the prior
  state without publishing.
- When the active `TheKnowledge/` checkout is read-only for upstream
  maintenance, draft the request first under `ECRs/TheKnowledge/open/` in
  the consuming project so the handoff stays reviewable before it reaches a
  writable TheKnowledge checkout. Move the record to
  `ECRs/TheKnowledge/in-progress/` when active upstream handling begins, and
  move it to `ECRs/TheKnowledge/closed/` when a TheKnowledge proposal,
  implementation, rejection, or deferral record resolves it. Closed records
  should note which TheKnowledge record or commit settled the request.
- The `Feedback` branch is only for cross-project feedback flowing back
  into TheKnowledge. Direct maintenance of TheKnowledge itself should keep
  using its normal internal trees on `trunk`.
- Projects may keep proprietary or third-party knacks in the consuming
  project's top-level `knacks/` directory, separate from the stock knacks
  under `TheKnowledge/knacks/`.
- Validate changed knack files with
  `python TheKnowledge/scripts/validate_knacks.py --project-root .`.
- Knack validation should stay lightweight: malformed Markdown and
  high-entropy findings are errors, word-count overruns are warnings, the
  validator uses `.git/knack-validation-cache.json` when a writable
  Git-backed cache path is available and
  `.cache/knack-validation-cache.json` otherwise, and path collisions with
  stock knacks should warn while still evaluating both files.
- Prefer real local implementations when they are deterministic, cheap, and
  safe. Prefer deterministic fixtures and fakes before dynamic mocks.
- Reserve mocks for narrow external services, unsafe side effects, expensive
  resources, nondeterministic dependencies, and hard-to-trigger failure
  paths. Pair important mocked behavior with real integration, contract,
  smoke, browser, or scripted coverage.
- Avoid mocking owned application logic in integration, contract, smoke,
  browser, and end-to-end tests unless the owned boundary is unsafe or
  unavailable.
- When updating the TheKnowledge submodule itself, prefer
  `python TheKnowledge/scripts/update_theknowledge_submodule.py`
  `--project-root . --knowledge-root TheKnowledge`. That helper fetches
  and briefly summarizes the incoming upstream `trunk` delta, adopts the
  reviewed version, runs the drift report, refreshes the managed starter
  files when needed, and leaves a reviewable parent-repo diff.
- For a manual managed-file check without adopting a new upstream commit, run
  `python TheKnowledge/scripts/report_managed_agents_drift.py`
  `--project-root . --knowledge-root TheKnowledge`.
- Follow `tool_execution_constraints.json` when it marks a tool-and-
  environment combination unsafe to parallelize. In a matched Codex sandbox,
  run Black only through
  `python TheKnowledge/scripts/run_tool_with_timeout.py black`; the
  harness will fall back to exactly one file at a time when Black would
  otherwise hit the known multi-file hang. Do not assume `black -W 1` is
  enough. If another file-safe formatter or linter still hangs inside a
  Codex sandbox on a multi-file run, retry the explicit file list one file at
  a time, and rerun the full required checks outside the affected sandbox or
  in CI before clearing the work.
<!-- THEKNOWLEDGE_MANAGED_FOOTER_END -->
