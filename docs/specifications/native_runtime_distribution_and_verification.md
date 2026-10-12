# Native Runtime Distribution and Verification

## Purpose

Install Claude Code into a project as a pinned, verified native binary
fetched directly from Anthropic's release distribution, with no Node.js or
npm dependency, on Windows, macOS, and Linux. This specification replaces
`bounded_managed_npm_operations.md` for every new install.

## Scope

Release-channel resolution, manifest and signature verification, platform
selection, binary download, structural and smoke validation, retained
evidence, notify-only update checks, and network/TLS behavior. Slot layout
and promotion are governed by `ab_runtime_slots.md`; migration from the npm
layout by `legacy_npm_layout_migration.md`.

## Background

Anthropic publishes each Claude Code release under
`https://downloads.claude.ai/claude-code-releases/`:

- `/stable` and `/latest`: plain-text version pointers (unsigned).
- `/<version>/manifest.json`: per-platform `binary`, `checksum` (SHA-256),
  and `size`, plus `version`, `commit`, and `buildDate`.
- `/<version>/manifest.json.sig`: a documented OpenPGP v4 detached
  signature (RSA-4096, SHA-512) by key fingerprint
  `31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE`, present from 2.1.89.
- `/<version>/manifest.json.raw-sig.json`: an RSASSA-PKCS1-v1_5-SHA512
  signature over `b"claude-code-manifest-v1\x00" + manifest bytes`, with
  `publicKeySha256` naming the signing key, present from 2.1.207.
- `/<version>/<platform>/claude[.exe]`: the self-contained binary.

Anthropic documents fetching from this bucket with manifest verification,
and documents `DISABLE_UPDATES` for distribution through other channels.
The npm package is a wrapper around the same bytes and is not used.

## Required Behavior

1. **Node-free operation**

   Install, upgrade, update, repair, inspect, selftest, uninstall, and the
   generated launcher must not require `node`, `npm`, or `npx`. Diagnostics
   may still report them.

2. **Channel resolution**

   Supported channels are `stable` and `latest`. `next` is accepted as a
   deprecated alias of `latest` and must print a deprecation warning. A
   channel pointer is valid only when it matches `^\d+\.\d+\.\d+$` after
   stripping surrounding whitespace, and it is never trusted for anything
   except choosing which signed manifest to fetch.

3. **Version floor**

   The minimum installable version is 2.1.207, the first release with both
   signature forms and the documented custom-launcher behavior that
   `DISABLE_UPDATES` relies on. Exact pins below the floor must be refused
   with a message that names the floor. There is no unsigned mode.

4. **Pinned trust anchors**

   The RSA public key (modulus and exponent), its SPKI SHA-256, and the
   OpenPGP fingerprint must be compiled into the installed claude-wrangler
   package. They must never be read from the consuming repository, the
   network, or the operator's GnuPG keyring, and `gpg` must never be
   invoked.

5. **Manifest verification before parsing**

   The manifest bytes must be verified before they are parsed as JSON:
   - the OpenPGP signature must be a v4 signature of type 0x00 using RSA
     and SHA-512, whose issuer fingerprint equals the pinned fingerprint
     and whose RSA value verifies against the pinned key; and
   - the raw signature must use `RSASSA-PKCS1-v1_5-SHA512`, its
     `publicKeySha256` must equal the pinned SPKI hash, and it must verify
     over the prefixed bytes.

   Both must pass. The raw signature must be exactly the modulus length
   `k`; the OpenPGP MPI must be minimally encoded, at most `k` octets, and
   left-padded to `k`. Verification must check `s < n` and compare the
   complete encoded block. It
   must use explicit `raise` statements, never `assert`, so `-O` cannot
   disable it.

6. **Manifest content checks**

   After verification the manifest must parse as a JSON object whose
   `version` equals the requested version and which contains the selected
   platform key with a 64-hex-digit `checksum` and a positive integer
   `size`. `manifestSignatureEnforcement` and any other manifest field
   must never relax claude-wrangler's own policy.

7. **Platform selection**

   The platform key must be one of the manifest's keys and must reflect
   the hardware, not the Python process:
   - macOS: `darwin-arm64` when `sysctl.proc_translated` is 1 or the
     machine is arm64; otherwise `darwin-x64`.
   - Windows: `win32-arm64` when the native machine is ARM64 (via
     `IsWow64Process2` or `PROCESSOR_ARCHITEW6432`/`PROCESSOR_ARCHITECTURE`);
     otherwise `win32-x64`.
   - Linux: `linux-arm64` or `linux-x64`, with a `-musl` suffix when the
     system C library is musl (the same file checks as Anthropic's
     `install.sh`), never inferred from `platform.libc_ver()` alone.

   `--platform` may override detection (for example, to choose
   `win32-x64` under emulation on ARM64); an override must be one of the
   manifest's keys for the host operating system. Installing a foreign
   operating system's build is refused: run claude-wrangler on that
   system (for example, inside WSL) instead, so the structural
   interpreter check and the smoke test always run.

8. **Bounded, verified download**

   Downloads must use HTTPS only. Redirects must stay on HTTPS and on an
   allowlisted host (`downloads.claude.ai` by default; an explicit
   `--release-base-url` adds its host). The binary must be streamed into a
   staging file on the same volume as the slots, hashing as it goes. The
   transfer must abort once received bytes exceed the manifest `size`, and
   the final byte count and SHA-256 must equal the manifest. Interrupted
   transfers may resume with an HTTP Range request against the same
   staging file. Resume applies only within one transaction's bounded
   retries: every transaction stages into a fresh `.staging/<txid>/`, so
   a rerun after a failed install downloads from the first byte. Each
   request must have a connect/read timeout
   (`--download-timeout-seconds`, default 60 s per read) and a small
   bounded retry count with backoff. Progress goes to stderr.

9. **Structural check**

   Before promotion the file must match the platform's executable format:
   PE with the expected machine type on Windows, Mach-O with the expected
   CPU type on macOS, and ELF with the expected `e_machine` on Linux. For
   ELF, the program interpreter path must exist on the host.

10. **Smoke test**

    The candidate must run `--version` and print exactly
    `<version> (Claude Code)` within 60 seconds (first-run antivirus scans
    are slow). The smoke test must run with `CLAUDE_CONFIG_DIR`, `HOME`,
    `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `XDG_*`, `TMP`, `TEMP`, and
    `TMPDIR` pointed into transaction scratch, and with the self-update
    suppression variables set. It must never run with the operator's real
    home or the project's config dir.

11. **Retained evidence**

    Each promoted slot must keep `manifest.json`, `manifest.json.sig`,
    `manifest.json.raw-sig.json`, and a completion record (see
    `ab_runtime_slots.md`). Repair and selftest re-verify a slot offline
    from this evidence, and so does every operation that keeps or
    reuses a slot (`ab_runtime_slots.md` requirement 15): a slot is never
    trusted on its completion record alone, because a repository can
    supply one. A release host other than the default that came from
    recorded metadata is named before it is contacted, and install-like
    operations refuse metadata that git tracks; signatures still bind
    every byte, so such metadata could otherwise only pin an older signed
    release or redirect the update check's traffic.

12. **Notify-only update checks**

    claude-wrangler, not Claude Code, checks for newer releases:
    - `--update` fetches both channel pointers, verifies the signed
      manifest for each newer version, records the result (version,
      `buildDate`, checked-at time) in metadata, and never changes the
      installed binary.
    - `update-check.json` keeps two values apart. `latest_version` holds
      only a version whose signed manifest verified; only `--update`
      writes it. `pointer_version` holds the most recent unsigned channel
      pointer read. The launcher's own refresh writes `pointer_version`
      and carries `latest_version` forward unchanged, so an unsigned
      pointer can never pass for verified data (requirement 2).
    - The launcher shows a one-line notice when the cache names a newer
      version on the installed channel. A newer `latest_version` is
      announced as available. Otherwise a newer `pointer_version` is
      announced as unverified, and the notice says that `--upgrade`
      verifies its signed manifest before installing it. The launcher may
      refresh the cache itself at most once per 24 hours, in the
      background or with a 3-second total budget, and must never block
      or fail a launch because of the network.
    - `claude-local --check-updates` forces a refresh and prints the
      unsigned pointer, the verified version, and the installed version
      separately; for a pointer-only newer release it names both
      `--upgrade` (verify and install) and `--update` (verify without
      installing). The launcher uses the release base URL recorded in
      `launch.json`, and `update-check.json` records any refresh error
      (`error`, null after a successful refresh) so `--inspect` can tell
      a failed check from a successful one. Update checks can be disabled
      per project (`--set-update-checks off`).
    - Notices must tell the operator to run
      `claude-wrangler --upgrade --channel <channel>`; nothing installs
      automatically.

13. **TLS and proxies**

    The downloader must honor `HTTPS_PROXY`/`NO_PROXY`, `SSL_CERT_FILE`,
    and an explicit `--ca-bundle`. TLS failures must name the presented
    issuer when available and, on macOS python.org builds, mention the
    "Install Certificates" step. `--ca-bundle` applies to the
    claude-wrangler run that receives it and is not recorded: the
    launcher's own update-check refresh uses Python's default TLS context
    (the system trust store plus `SSL_CERT_FILE`) and `HTTPS_PROXY`/
    `NO_PROXY`. Behind a TLS-inspecting proxy whose CA is only available
    through `--ca-bundle`, the launcher's refresh fails, records the
    error, and never blocks the launch; `--update` with `--ca-bundle`
    still refreshes the verified catalog.

14. **Network budget for the bootstrap path**

    The fetch and verification code must remain Python 3.9-compatible and
    standard-library-only (no `hashlib.file_digest`, no `tarfile`
    extraction filters, no zstd).

## Validation

- Signature tests against recorded manifests and signatures for at least
  two real releases, plus tampered-byte, wrong-key, wrong-fingerprint,
  wrong-length, `s >= n`, and `python -O` cases; no network.
- Platform-detection tests for Rosetta, musl, Windows ARM64 under x64
  emulation, and overrides.
- Download tests with a local HTTPS-free fake transport covering
  truncation, oversize, hash mismatch, resume, redirect to HTTP, and
  redirect to a non-allowlisted host.
- Structural-check tests for each executable format and mismatches.
- Smoke-test tests proving the scratch environment never contains the
  operator's real home or the project config dir.
- Update-check tests for notice rendering, the 24-hour cache, offline
  behavior, the disable switch, and a launcher refresh that never turns
  an unsigned pointer into `latest_version`.
