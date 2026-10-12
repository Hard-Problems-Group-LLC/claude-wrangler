# A/B Runtime Slots

## Purpose

Keep a project's Claude Code runtime usable through failed, interrupted,
or concurrent installs, upgrades, and repairs, and give the operator an
instant rollback and an automatic launch-time fallback. The design adapts
codex-wrangler's `atomic_ab_managed_installation.md` to single-file native
binaries.

## Scope

The runtime root layout, slot contents, the active-slot pointer,
completion records, maintenance locking, promotion, fallback, rollback,
retirement of displaced binaries, in-use detection, and uninstall
ordering. Download and verification rules are in
`native_runtime_distribution_and_verification.md`.

## Layout

```
.local/claude/                         CLAUDE_CONFIG_DIR (Claude-owned)
  HOME/                                ring-1 redirected home
  wrangler/                            runtime root (--local-dir)
    .claude-wrangler.json              project metadata, schema 2
    .lock                              maintenance lock (never removed)
    active-slot                        "a\n" or "b\n"
    slots/a/                           one verified release
      <platform>/claude[.exe]
      manifest.json
      manifest.json.sig
      manifest.json.raw-sig.json
      .complete.json                   completion record
    slots/b/                           the other release (rollback target)
    .staging/<txid>/                   candidate being prepared
    .retired/<txid>/                   displaced slot awaiting deletion
    launch.py                          generated launcher core
    shim/                              PATH shim for nested `claude` calls
    update-check.json                  cached update-check result
```

The runtime root must not be, contain, or sit inside `HOME/`, any
`*/claude/versions` directory reachable from `HOME` or `XDG_*` (Claude
Code's housekeeping prunes those), or the config dir's `local/`,
`downloads/`, or `cache/` children (Claude Code owns those names).

## Required Behavior

1. **Slots**

   Each slot holds exactly one verified release for one or more platform
   keys, its retained manifest evidence, and a completion record. A slot
   is complete only when its completion record exists, parses, names the
   slot, and its binary's size matches the record.

2. **Active pointer**

   `active-slot` is a regular file containing exactly `a` or `b` and one
   LF. The same form is used on every operating system so a checkout
   behaves identically on Windows, macOS, Linux, and WSL. It is replaced
   only through a same-directory temporary file and `os.replace`. A
   pointer naming an incomplete slot is invalid.

3. **Completion record**

   `.complete.json` records schema version, slot name, version, channel,
   platform keys with binary relative path, SHA-256, and size, manifest
   SHA-256, `buildDate`, `commit`, signature schemes verified, verified-at
   time, and the claude-wrangler version that promoted it. It is written
   atomically only after every validation step passes.

4. **Maintenance lock**

   Install, upgrade, repair, rollback, update, reconfiguration,
   migration, and uninstall hold `.lock` from state selection through
   publication of generated files. The lock uses an OS file lock
   (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere) on a file that
   is never deleted. A second maintainer must fail fast with a clear
   message and change nothing.

5. **Inactive-only preparation**

   Downloads and validation happen in `.staging/<txid>/`. Nothing in the
   active slot, the pointer, metadata, or generated files changes until
   the candidate passes validation.

6. **In-use detection**

   Before an occupied inactive slot is displaced, before rollback to a
   slot, before uninstall, and before migration cleanup, claude-wrangler
   must determine whether any running process image lives under the
   runtime root:
   - Windows: compare process image paths (via the Toolhelp/`QueryFullProcessImageNameW` APIs through `ctypes`) case-insensitively.
   - Linux: resolve `/proc/<pid>/exe`.
   - macOS: use `ps -axo pid=,comm=` or `lsof`, whichever is available.

   It must first ask the project's Claude Code background service to stop
   (`<active binary> daemon stop` run with the project's launcher
   environment) because that service otherwise outlives sessions and
   falls back to the operator's global binary when its executable path
   disappears. If any process still runs from the slot to be displaced,
   the operation must stop without changes and name the process IDs.

7. **Promotion**

   With the lock held and the inactive slot free:
   1. move any existing inactive slot to `.retired/<txid>/`;
   2. rename the staged candidate to `slots/<inactive>/`;
   3. re-check the moved binary's size and completion record;
   4. replace `active-slot` (the commit point);
   5. write metadata and generated files atomically;
   6. delete `.retired/<txid>/` when nothing runs from it, otherwise leave
      it for a later repair.

   The formerly active slot is kept as the rollback target. Any failure
   before step 4 must restore the retired slot and leave the previous
   runtime, pointer, metadata, and generated files unchanged.

8. **Launch-time fallback**

   The launcher must verify the active slot cheaply on every launch
   (completion record present and parseable, binary present, size equal
   to the record). If the active slot fails and the other slot passes,
   the launcher must print a prominent warning naming both versions and
   the failure, run the other slot, and record the event in
   `wrangler/fallback.json` for `--inspect` and `--repair-install`. If
   neither slot passes, it must refuse with instructions to run
   `claude-wrangler --repair-install`. It must never fall back to a
   global `claude`.

9. **Rollback**

   `claude-wrangler --rollback` makes the other slot active after the
   in-use check and a full offline re-verification of that slot.

10. **Repair**

    `--repair-install` clears `.staging/`, deletes `.retired/` entries no
    longer in use, re-verifies both slots offline against their retained
    evidence, re-downloads the pinned version into the inactive slot when
    the active slot is missing or corrupt, and repairs an invalid
    pointer by choosing the complete slot whose version matches metadata.
    It never touches anything outside the runtime root except generated
    files.

11. **Retention**

    At most two releases are installed per project: the active slot and
    the rollback slot. Platform keys for the same release may share a
    slot.

12. **Uninstall ordering**

    Uninstall must stop the background service, prove no process runs
    from the runtime root, and only then remove the runtime, generated
    files, and (per existing conservative rules) session state. If a
    binary cannot be deleted, uninstall must stop before touching
    session state and must keep the `.gitignore` block.

13. **Windows file semantics**

    On Windows a running executable can be renamed but not overwritten or
    deleted, and antivirus scanners briefly hold new executables. Renames
    and deletes inside the runtime root must retry `PermissionError`
    (WinError 5 and 32) with backoff for up to 60 seconds before failing.

14. **Line endings and atomic writes**

    Every generated file is written through a same-directory temporary
    file and `os.replace`, with explicit LF line endings for files read by
    POSIX shells and CRLF for `.cmd` files.

## Validation

- Promotion `a`→`b` and `b`→`a`, with injected failures before and after
  the commit point.
- Concurrent maintainers: the second fails fast and changes nothing.
- In-use refusal for the slot to be displaced, using a fake process
  table.
- Launch fallback when the active binary is missing, truncated, or its
  record is corrupt; refusal when both slots fail.
- Rollback and repair, including pointer repair and retired-slot cleanup.
- Uninstall that stops before session state when the runtime is locked.
