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
      .verified.json                   verification stamp (binary identity)
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
   keys, its retained manifest evidence, a completion record, and a
   verification stamp. A slot is complete only when its completion record
   exists, parses, names the slot, and its binary's size matches the
   record. A slot is verified only when, in addition, both retained
   signatures verify, the manifest hash equals the record's, every
   recorded binary's SHA-256 and size equal the signed manifest's, and
   the slot holds nothing else: no file or directory beyond the record,
   the stamp, the three evidence files, and each recorded binary inside
   the directories leading to it, and no link or junction anywhere. On
   Windows a DLL beside an executable is loaded before the system copy,
   so an extra file could change what a verified binary runs. Finder's
   `.DS_Store` and the temporary sibling a killed atomic write leaves are
   tolerated.

2. **Active pointer**

   `active-slot` is a regular file containing exactly `a` or `b` and one
   LF. The same form is used on every operating system so a checkout
   behaves identically on Windows, macOS, Linux, and WSL. It is replaced
   only through a same-directory temporary file and `os.replace`. A
   pointer naming an incomplete slot is invalid.

3. **Completion record**

   `.complete.json` records schema version, slot name, version, channel,
   platform keys with binary relative path, SHA-256, and size, manifest
   SHA-256, `build_date`, `commit`, the binary's subcommand names (read
   from `--help` run in scratch), `settings_rejected` (the subcommands
   that refused a leading `--settings` in the install-time probe of
   `cross_platform_launcher.md` requirement 4), signature schemes
   verified, verified-at time, and the claude-wrangler version that
   promoted it. It is written atomically only after every validation step
   passes.

4. **Maintenance lock**

   Install, upgrade, repair, rollback, update, reconfiguration,
   migration, and uninstall hold `.lock` from state selection through
   publication of generated files. The lock uses an OS file lock
   (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere) on a file that
   is never deleted, except as the last step of uninstall, after the
   lock has been released. A second maintainer must fail fast with a
   clear message and change nothing.

   Command-line configuration is derived from the native and npm-era
   metadata before the lock is taken, so the configuration records a
   digest of the metadata files it read. Once the lock is held, every
   maintenance operation recomputes that digest before changing
   anything and stops with `ManagedStateChangedError` when another
   maintainer has published since. The CLI then re-reads the state and
   reruns the command, up to three attempts in all, and otherwise asks
   the operator to rerun once the other run has finished. A stale read
   therefore never reverts another maintainer's publish. The digest is
   computed after the configuration is derived and the metadata is then
   read again; when the second read differs, the command stops with
   `ManagedStateChangedError` before taking the lock, so the digest
   always describes the state the configuration was derived from.

   Maintenance operations write the managed `.gitignore` block only
   while holding the lock, so a refused second maintainer leaves it
   untouched. Inspect and selftest take no lock and still enforce the
   block (`managed_gitignore_enforcement.md` requirement 1), as do the
   `--dry-run` forms of install, upgrade, repair, and update, which take
   no lock.

   Uninstall takes the lock before its ownership and in-use checks and
   holds it through runtime, generated-file, and session-state removal;
   only the lock file and the directories leading to it remain until
   the lock is released. Uninstall deletes the lock file only while it
   still owns it: on POSIX it unlinks the file while holding the lock,
   and only when the path still names the locked inode; on Windows,
   which refuses to delete a file another process holds open, it
   deletes the file once after closing it and never retries. It then
   removes the runtime root only when nothing else appeared there, and
   otherwise stops, keeping the `.gitignore` block. If uninstall stops
   early, it removes the empty runtime-root directories that only it
   created, never a lock file another maintainer holds. On POSIX every
   maintainer, after locking, also proves that the lock path still names
   the inode it locked, so none ever proceeds on an orphaned inode. A
   lock attempt that fails for a reason other than contention (`ENOLCK`,
   `EINVAL`, or `ENOTSUP` on a filesystem without locks) is reported as
   such, not as another maintainer. Nobody can hold a lock there, so
   when such an uninstall created the runtime root itself, it also
   deletes the lock file that attempt created before removing the empty
   directories; a lock file that existed before is kept. A runtime root
   that is a link or junction makes uninstall fail with the lock error
   before anything is removed.

5. **Inactive-only preparation**

   Downloads and validation happen in `.staging/<txid>/`. Nothing in the
   active slot, the pointer, metadata, or generated files changes until
   the candidate passes validation.

6. **In-use detection**

   Before an occupied inactive slot is displaced, before rollback to a
   slot, before uninstall, and before migration cleanup, claude-wrangler
   must determine whether any running process image lives under the
   runtime root:
   - Windows: compare process image paths (via the Toolhelp/`QueryFullProcessImageNameW` APIs through `ctypes`) case-insensitively. An image reported through an 8.3 short path (`LONG-P~1`) is expanded to its long name (`GetLongPathNameW`) before the comparison, and the launcher starts the binary by its long-name path.
   - Linux: resolve `/proc/<pid>/exe`.
   - macOS: ask `libproc`'s `proc_pidpath` through `ctypes`. It reports the executable's vnode path as the kernel sees it now: physical, and current after a directory rename, so a process running from a slot moved to `.retired/` is still found. `ps -axo pid=,comm=` is only a fallback when `libproc` cannot be loaded; it reports `argv[0]`, which can run through a symbolic link, so an absolute image is also compared in its resolved spelling.

   When the process list itself cannot be read, the operation fails
   closed.

   If any process runs from the slot to be displaced, the operation must
   stop without changes and name the process IDs. It must never stop
   Claude Code's background service on its own, because
   `daemon stop --any` also terminates the operator's background
   sessions. With the explicit `--stop-background-sessions` flag it runs
   `<binary> daemon stop --any` with the project's launcher environment
   and then repeats the check. A `--dry-run` never stops the service; it
   reports the stop it would run. Because a slot is never displaced while
   in use, the background service can never lose its executable path,
   which would otherwise make it fall back to the operator's global
   binary. Inspect and selftest must not read `.lock`, because on
   Windows a held byte-range lock makes reads fail.

7. **Promotion**

   With the lock held and the inactive slot free:
   1. move any existing inactive slot to `.retired/<txid>/`;
   2. rename the staged candidate to `slots/<inactive>/`;
   3. re-check the moved binary's size and completion record, then write
      the verification stamp for this host's platform keys;
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
   to the record, and the binary's identity equal to the verification
   stamp; see requirement 15). If the active slot fails and the other
   slot passes,
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
    longer in use, and re-verifies both slots offline against their
    retained evidence (signatures, binary SHA-256, and the exact file set
    of requirement 1). A slot that verifies is stamped again, so a copied
    or restored checkout launches again without a download; a slot that
    fails loses its stamp, so no launcher falls back to it. It then
    chooses the slot that should be active:
    - when the pointer names a slot that verifies, that slot stays
      active;
    - otherwise the pointer moves, before any download, to a verified
      slot, preferring the one whose version matches the metadata pin.
      When that slot is older or newer than the pin, repair warns that
      the project runs it until the pinned version is re-downloaded,
      and that it keeps running it if the download fails;
    - when the active slot does not hold the pinned version for this
      host's platform, the pinned version is downloaded, validated, and
      promoted into the slot the pointer does not name. After a pointer
      move that is the corrupt slot; with no verified slot at all it is
      the slot other than the one the invalid pointer names (slot `a`
      when there is no pointer). When that slot already holds the pinned
      release and verifies, the pointer moves to it instead of a
      download. A plain install does the same, so finishing an
      interrupted upgrade, or returning to the pin, never downloads a
      release a slot already holds.

    Repair clears `fallback.json` once it succeeds and regenerates
    generated files. Outside the runtime root it touches only generated
    files, the config dir's `settings.json` merge
    (`cross_platform_launcher.md` requirement 4), and a deferred npm-era
    cleanup retry with its schema-1 projection
    (`legacy_npm_layout_migration.md` requirement 5). Install and upgrade
    perform the same three.

11. **Retention**

    At most two releases are installed per project: the active slot and
    the rollback slot. Platform keys for the same release may share a
    slot. When a release is installed again into a new candidate, other
    platforms' binaries for that release are carried over from the
    active slot and from the slot being replaced, but only when each
    one's SHA-256 still matches the signed manifest.

12. **Uninstall ordering**

    Uninstall must prove no process runs from the runtime root (stopping
    the background service only with `--stop-background-sessions`, as in
    requirement 6), and only then remove the runtime, generated
    files, and (per existing conservative rules) session state. Inside
    the runtime root it removes `.staging/`, `.retired/`, and `slots/`
    first and the metadata last, and in an npm-era directory the npm tree
    first and its metadata last, so the ownership proof a rerun needs
    survives a partial failure. If a binary cannot be deleted, uninstall
    must stop before touching session state, must keep the `.gitignore`
    block, and a plain rerun (without `--force`) finishes the removal.
    A file that cannot be rendered for the layout (a non-ASCII path in
    the ASCII-only cmd launcher) never blocks uninstall, inspect, or
    selftest. On Windows, uninstall (and its dry run) ends with a note
    that Claude Code may keep the project's login in Windows Credential
    Manager under a generic credential whose name is derived from the
    exact config-dir path; uninstall cannot see or remove it, and a
    later checkout at the same path would find it, so the note points
    to Claude Code's `/logout` and to Credential Manager.

13. **Windows file semantics**

    On Windows a running executable can be renamed but not overwritten or
    deleted, and antivirus scanners briefly hold new executables. Renames
    and deletes inside the runtime root must retry `PermissionError`
    (WinError 5 and 32) with backoff for up to 60 seconds before failing.

    Windows programs without long-path support fail to start from a path
    of 260 characters or more: a real `claude.exe` failed with
    WinError 206 from a 272-character staging path. Staging directories
    therefore use 12-hex-digit transaction names, and claude-wrangler
    warns, before the smoke test, when a staged Windows binary path is
    240 characters or longer, suggesting a shorter checkout path.

14. **Line endings and atomic writes**

    Every generated file is written through a same-directory temporary
    file and `os.replace`, with explicit LF line endings for files read by
    POSIX shells and CRLF for `.cmd` files.

15. **Verified reuse and the verification stamp**

    Nothing a repository can supply is trusted as verified. A repository
    that commits `.local/claude/...` (past the managed `.gitignore` block
    with `git add -f`) supplies a binary, its completion record, the
    pointer, and the metadata pin; the record-and-size check of
    requirement 1 accepts such a slot.
    - Every operation that keeps, reuses, or activates a slot (install's
      "already installed" path, migration's reuse of a slot, the settings
      toggles and `--skip-install`, `--update`, rollback, repair, and the
      activation of requirement 10) verifies it in full first. A slot
      that fails is never reused; install and migration put a verified
      release into the other slot instead, and the offline operations
      refuse. A recorded pin is copied from an active slot only when it
      verifies.
    - After a full verification, maintenance writes
      `slots/<x>/.verified.json`: per platform key of this host's
      operating system, the binary's size, modification time in
      nanoseconds, and on POSIX its inode. Stamps for another operating
      system's keys (a checkout shared with WSL) are kept. The binaries'
      identities are read before hashing and compared afterwards. A slot
      whose integrity fails loses its whole stamp; a slot that is intact
      but lacks this host's platform keeps it.
    - The launcher runs a binary only while its identity equals the
      stamp. A git clone or `git pull` writes new files with a fresh
      modification time and inode, so a binary it planted or replaced
      cannot match. Windows inode values differ between Python versions
      on ReFS, so the Windows stamp holds size and modification time
      only; an archive or copy tool that preserves modification times
      could therefore restore a same-size binary together with a
      matching stamp, which is why tracked runtime state is refused
      (below) and repair re-verifies in full. A copied or restored
      checkout whose tool writes new modification times fails the check
      until `--repair-install` re-verifies and re-stamps it offline.
      Each operating system stamps only its own keys, so after
      maintenance on one side of a shared WSL checkout, the other side
      runs a plain install (or repair) once before it launches.
    - Install-like operations (including the settings toggles and
      `--skip-install`), `--update`, and rollback refuse when
      `git ls-files` reports tracked files under the runtime root, the
      config dir, the redirected home, the npm-era directory, or the
      launchers, unless `--allow-tracked-state` is given; inspect and
      selftest report them. Each pathspec carries `literal` magic, and
      on a case-insensitive filesystem (Windows, default macOS, or a
      project root whose `.git` answers to `.GIT`) also `icase`, because
      there a tracked `.LOCAL/Claude/...` is the managed
      `.local/claude/...` while git compares pathspecs by case. Git's
      global pathspec variables are cleared for the query, since
      `GIT_LITERAL_PATHSPECS` would make every pathspec match nothing.
      Without git, or outside a work tree, nothing can be tracked and
      the check passes.
    - A release host other than the default that came from recorded
      metadata (not from `--release-base-url` on the command line) is
      named before it is contacted and in the install summary.
    - A `--local-dir` that names an existing directory holding none of
      the runtime root's own entries is refused, because uninstall
      empties the runtime root. Every managed path must render into the
      generated files (ASCII and no double quote in the cmd launcher, no
      control characters anywhere); both are checked before anything is
      downloaded.

## Validation

- Promotion `a`→`b` and `b`→`a`, with injected failures before and after
  the commit point.
- Concurrent maintainers: the second fails fast and changes nothing,
  including the `.gitignore` block, for every maintenance operation.
- Stale state: a maintainer whose metadata changed after it was read
  stops before changing anything, and the CLI reruns it, giving up after
  three attempts.
- In-use refusal for the slot to be displaced, using a fake process
  table.
- Launch fallback when the active binary is missing, truncated, or its
  record is corrupt; refusal when both slots fail.
- Rollback and repair, including pointer repair and retired-slot cleanup.
- Uninstall that stops before session state when the runtime is locked,
  keeps the metadata, and finishes on a plain rerun; a dry run that
  never stops background sessions.
- A planted slot (record and size match, bytes do not) is never reused
  by install or migration and never launched; a binary replaced after
  verification is refused at launch; repair re-stamps without a
  download; tracked runtime files are refused; a DLL or other extra
  file in a slot fails verification, selftest, and repair.
- An interrupted publish followed by a plain install activates the
  inactive slot instead of downloading.
- 8.3 short image paths on Windows, and `libproc` and `ps` parsing with
  injected fakes for macOS and Linux.
