# Testing on a Fresh Windows with Windows Sandbox

## Purpose

Record how to use Windows Sandbox as a disposable, never-signed-in
Windows for claude-wrangler checks, what it showed on 2026-10-12, and
the platform defect that limits it, so later sessions neither rediscover
the defect nor repeat the dead ends.

Use it for questions that need a fresh Windows or a place where running
the real Claude Code binary has nothing at stake:

- behavior that depends on a never-used Windows (for example, the root
  certificate store before any Windows TLS request);
- Claude Code's own updater and housekeeping, which must never run
  against the operator's real profile (ring 0 is operator-run only; see
  `security_ring_model.md`).

It is not a substitute for the unit suite (`scripts/run_tests_isolated.py`)
or for the live end-to-end checks on the host with every home redirected.

## Known Defect: One Working Launch Per Boot

On the development host (Windows 11 26H2, build 26300, Windows Sandbox
app 0.8.107.0), only the first launch after a boot reliably reaches the
desktop. Later launches show a black screen, a spinner, and then "The
connection to the Windows Sandbox environment was lost. Do you want to
reconnect?", in a loop. Reconnecting does not help.

This is an open upstream bug with no Microsoft response as of
2026-10-12:

- [microsoft/Windows-Sandbox#103](https://github.com/microsoft/Windows-Sandbox/issues/103):
  the first launch after a reboot works, later ones fail with the
  connection-lost dialog. Reported mostly on hosts with both an
  integrated and a discrete GPU, which the development host has.
- [microsoft/Windows-Sandbox#128](https://github.com/microsoft/Windows-Sandbox/issues/128):
  the same dialog on app 0.8.107.0, failing about 90% of the time.

What was established locally:

- 2 of about 15 launches before the evening reboot reached logon. After the
  reboot, the first launch of a bare configuration worked and the next
  two, minutes later, failed the same way.
- The configuration made no difference: a bare `.wsb` (one empty mapped
  folder, a `cmd /c echo` marker) failed exactly like the full test
  configuration. `<VGpu>Disable</VGpu>` made no difference either.
- No pending update, low disk space, or leftover VM explained it. The
  VHD "failed to create handle" events in
  `Microsoft-Windows-VHDMP-Operational` also appear in successful
  launches, so they are noise.

Workarounds reported upstream, none reliable: pinning the sandbox app to
one GPU in Settings > System > Display > Graphics, disabling the
integrated GPU, or restarting the Hyper-V Host Compute Service
(`vmcompute`, admin). Changing GPU settings is the operator's call.

Practical rule: reboot, then run the real test as the first launch.
Do not spend that launch on a smoke test.

### Recognizing the Failure

The sandbox client is a Remote Desktop client, and its log shows the
failure without waiting for a timeout. In
`Microsoft-Windows-TerminalServices-RDPClient/Operational`, a failed
launch logs event 1025 (connected) followed 23 to 30 seconds later by
event 1026 "disconnected (Reason= 3)"; a working launch stays
connected.

```powershell
Get-WinEvent -FilterHashtable @{
  LogName = 'Microsoft-Windows-TerminalServices-RDPClient/Operational'
  StartTime = (Get-Date).AddMinutes(-10); Id = 1024, 1025, 1026 } |
  Sort-Object TimeCreated | Format-Table TimeCreated, Id, Message -Wrap
```

## Running a Test

The host had no `wsb.exe`, and `hcsdiag` needs admin, so a run is a
configuration file, a launch, and a results folder polled from the host.

1. Make one scratch folder outside the checkout and copy into it
   everything the guest needs: a Python install (the guest has none), a
   Git for Windows install if the test uses Git, and the checkout under
   test (excluding `.git`, `.venv`, and `.local`). Map only that folder,
   read-write. Never map the real checkout, profile folders, or anything
   holding credentials: the guest can write to every mapped folder.
2. Write a driver script into the same folder. It should create its
   output folder first (so a run that never started is distinguishable
   from one that failed), record every command's exit code and output,
   write a `done` marker, and end with `shutdown /s /t 5` so the sandbox
   closes itself.
3. Launch with `Start-Process WindowsSandbox.exe <file>.wsb` and poll
   the output folder. Only one sandbox runs at a time.

```xml
<Configuration>
  <Networking>Enable</Networking>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>C:\path\to\scratch\sbx</HostFolder>
      <SandboxFolder>C:\sbx</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>C:\sbx\py\python.exe -I -B C:\sbx\driver.py</Command>
  </LogonCommand>
</Configuration>
```

Operating rules learned the hard way:

- Do not force-kill `WindowsSandboxRemoteSession`, `WindowsSandboxServer`,
  or `vmmemWindowsSandbox`. The VM lingers for minutes afterwards. Have
  the driver shut the guest down, or ask the operator to close the
  window.
- Before the next launch, wait until those three processes have exited.
- Write any `.wsb` or driver text containing Windows paths with an
  editor or a file-writing tool, not a shell heredoc, which can collapse
  backslashes (`C:\sbx\bisect` became a backspace once).
- The guest account is `WDAGUtilityAccount` with profile
  `C:\Users\WDAGUtilityAccount`. Fingerprint its Claude areas before and
  after each step to show what a step changed.

## What the Sandbox Showed (2026-10-12)

Observed with Claude Code 2.1.287, then 2.1.296 after an upgrade, in a
project installed by the checkout under test:

- Root certificates: a fresh Windows lacked `GlobalSign Root CA`, which
  `downloads.claude.ai`'s chain needs, so Python failed with "unable to
  get local issuer certificate". One Windows PowerShell
  `Invoke-WebRequest` made Windows fetch the root, after which Python
  verified the host. claude-wrangler now makes that request itself on
  exactly that failure and retries once per host (see the README's
  network section).
- Update suppression held in layers:
  - the launcher refused `update` and `--verbose update` (exit 2);
  - the slot's binary run directly with the launcher's environment
    printed "Updates are disabled by your administrator";
  - with those variables removed, the config-dir `settings.json` still
    blocked `update` and `install`;
  - a `--settings` override blocked the update even when a project
    settings file cleared those variables.
- Precedence observed: `--settings` beats a project
  `.claude/settings.json` `env` block, which beats the config-dir
  settings and the process environment. A project settings file that
  blanks `DISABLE_UPDATES` and friends therefore re-enabled the updater
  for a launcher-less nested call. It installed only into the project's
  redirected home; the slots and the guest profile were untouched, and
  `--selftest` still passed. The launcher warns about such a file at
  startup, and the README's Known Limitations describe it.
- Ring 1 and ring 0 (`--shared-home`) `-p` and `doctor` runs left the
  guest profile and a simulated global install (with planted old
  version entries) untouched; no pruning was observed.

Not yet verified in a fresh sandbox: the automatic root fetch itself
(two attempts hit the defect above). On the host, with one forced
missing-issuer failure, the real PowerShell request, store reload, and
retry worked against `downloads.claude.ai`.

## Alternative

For repeated fresh-Windows runs, a Hyper-V virtual machine installed
from the official Windows image, with a checkpoint to revert to, avoids
the sandbox's remote-session layer and its once-per-boot defect. It
needs a one-time install and, if Hyper-V Manager is not yet enabled, a
feature change and a reboot, which is the operator's decision.
