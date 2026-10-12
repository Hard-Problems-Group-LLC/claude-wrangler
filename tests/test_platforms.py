"""Tests for host platform detection and executable structure checks.

Every detection branch runs on any operating system through injected
``HostProbes`` values and probe callables. Structural checks run against
synthetic minimal PE, Mach-O, and ELF headers padded to the minimum size.
Set ``CLAUDE_WRANGLER_TEST_REAL_BINARY`` (and optionally
``CLAUDE_WRANGLER_TEST_REAL_BINARY_PLATFORM``) to also check a real
Claude Code binary; that test only reads its headers.
"""

from __future__ import annotations

import os
from pathlib import Path
import struct
import subprocess
import sys

import pytest

from claude_wrangler import platforms
from claude_wrangler.models import ClaudeWranglerError
from claude_wrangler.platforms import (
    MIN_EXECUTABLE_SIZE,
    PLATFORM_KEYS,
    HostProbes,
    PayloadError,
    PlatformError,
    binary_filename,
    check_executable_structure,
    describe_executable,
    detect_musl,
    detect_rosetta_translated,
    detect_windows_native_machine,
    elf_interpreter,
    host_platform_key,
    normalize_machine,
    resolve_platform_key,
)

CPU_X86_64 = 0x01000007
CPU_ARM64 = 0x0100000C
PE_X64 = 0x8664
PE_ARM64 = 0xAA64
EM_X86_64 = 62
EM_AARCH64 = 183
GLIBC_X64 = "/lib64/ld-linux-x86-64.so.2"
GLIBC_ARM64 = "/lib/ld-linux-aarch64.so.1"
MUSL_X64 = "/lib/ld-musl-x86_64.so.1"
MUSL_ARM64 = "/lib/ld-musl-aarch64.so.1"

# Offsets inside the synthetic PE built by build_pe().
PE_OFFSET = 0x80
PE_MACHINE = PE_OFFSET + 4
PE_SECTION_COUNT = PE_OFFSET + 6
PE_CHARACTERISTICS = PE_OFFSET + 22
PE_OPTIONAL = PE_OFFSET + 24
PE_SUBSYSTEM = PE_OPTIONAL + 68
PE_CERT_DIRECTORY = PE_OPTIONAL + 112 + 4 * 8
PE_SECTION_TABLE = PE_OPTIONAL + 240


def patch(data: bytes, offset: int, fmt: str, *values: object) -> bytes:
    """Return a copy of data with struct-packed values written at offset."""

    buffer = bytearray(data)
    struct.pack_into(fmt, buffer, offset, *values)
    return bytes(buffer)


def write_binary(
    directory: Path,
    header: bytes,
    size: int = MIN_EXECUTABLE_SIZE,
    name: str = "candidate",
) -> Path:
    """Write header bytes, zero-padded or cut to exactly size bytes."""

    path = directory / name
    with path.open("wb") as handle:
        handle.write(header[:size])
        if size > len(header):
            handle.truncate(size)
    return path


def build_pe(machine: int = PE_X64) -> bytes:
    """Return minimal PE32+ console-executable headers with one section."""

    buffer = bytearray(0x400)
    buffer[0:2] = b"MZ"
    struct.pack_into("<I", buffer, 0x3C, PE_OFFSET)
    buffer[PE_OFFSET : PE_OFFSET + 4] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", buffer, PE_MACHINE, machine, 1, 0, 0, 0, 240, 0x22)
    struct.pack_into("<H", buffer, PE_OPTIONAL, 0x20B)
    struct.pack_into("<I", buffer, PE_OPTIONAL + 60, 0x400)
    struct.pack_into("<H", buffer, PE_SUBSYSTEM, 3)
    struct.pack_into("<I", buffer, PE_OPTIONAL + 108, 16)
    buffer[PE_SECTION_TABLE : PE_SECTION_TABLE + 8] = b".text\0\0\0"
    struct.pack_into("<IIII", buffer, PE_SECTION_TABLE + 8, 0x200, 0x1000, 0x200, 0x400)
    return bytes(buffer)


def build_elf(
    machine: int = EM_X86_64,
    interpreter: object = GLIBC_X64,
    load: object = (0, 0x1000),
) -> bytes:
    """Return a minimal little-endian ELF64 executable header.

    ``interpreter`` may be a path string, raw bytes for the PT_INTERP
    segment, or None for a static binary; ``load`` is a (offset, size)
    PT_LOAD extent or None.
    """

    program_headers = []
    interpreter_bytes = b""
    if isinstance(interpreter, str):
        interpreter_bytes = interpreter.encode("utf-8") + b"\0"
    elif isinstance(interpreter, bytes):
        interpreter_bytes = interpreter
    if interpreter is not None:
        program_headers.append((3, 4, 0x200, len(interpreter_bytes)))
    if load is not None:
        program_headers.append((1, 5, load[0], load[1]))

    buffer = bytearray(0x400)
    buffer[0:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into(
        "<HHIQQQIHHHHHH",
        buffer,
        16,
        2,
        machine,
        1,
        0x401000,
        64,
        0,
        0,
        64,
        56,
        len(program_headers),
        64,
        0,
        0,
    )
    for index, (kind, flags, offset, size) in enumerate(program_headers):
        struct.pack_into(
            "<IIQQQQQQ",
            buffer,
            64 + index * 56,
            kind,
            flags,
            offset,
            0,
            0,
            size,
            size,
            1,
        )
    buffer[0x200 : 0x200 + len(interpreter_bytes)] = interpreter_bytes
    return bytes(buffer)


def build_macho(
    cpu_type: int = CPU_X86_64,
    magic: bytes = b"\xcf\xfa\xed\xfe",
    file_type: int = 2,
    segment: object = (0, 0x1000),
    entry: bool = True,
    code_signature: object = None,
) -> bytes:
    """Return a minimal little-endian 64-bit Mach-O executable header."""

    commands = []
    if segment is not None:
        commands.append(
            struct.pack(
                "<II16sQQQQiiII",
                0x19,
                72,
                b"__TEXT",
                0x100000000,
                segment[1],
                segment[0],
                segment[1],
                5,
                5,
                0,
                0,
            )
        )
    if entry:
        commands.append(struct.pack("<IIQQ", 0x80000028, 24, 0x1000, 0))
    if code_signature is not None:
        commands.append(struct.pack("<IIII", 0x1D, 16, *code_signature))
    body = b"".join(commands)
    header = magic + struct.pack(
        "<iiIIIII", cpu_type, 3, file_type, len(commands), len(body), 0, 0
    )
    return header + body


def build_fat(
    slices: list,
    fat64: bool = False,
    slice_size: int = 0x8000,
    table_cpu_types: object = None,
) -> bytes:
    """Return a universal Mach-O wrapping thin images at aligned offsets."""

    magic = b"\xca\xfe\xba\xbf" if fat64 else b"\xca\xfe\xba\xbe"
    table = b""
    placements = []
    offset = 0x4000
    for index, image in enumerate(slices):
        cpu_type = (
            table_cpu_types[index]
            if table_cpu_types is not None
            else struct.unpack_from("<i", image, 4)[0]
        )
        if fat64:
            table += struct.pack(">iiQQII", cpu_type, 0, offset, slice_size, 14, 0)
        else:
            table += struct.pack(">iiIII", cpu_type, 0, offset, slice_size, 14)
        placements.append((offset, image))
        offset += max(slice_size, 0x4000)
    buffer = bytearray(offset)
    header = magic + struct.pack(">I", len(slices)) + table
    buffer[0 : len(header)] = header
    for start, image in placements:
        buffer[start : start + len(image)] = image
    return bytes(buffer)


# --- Platform keys and binary names -------------------------------------


def test_platform_keys_match_release_manifest_keys():
    assert set(PLATFORM_KEYS) == {
        "darwin-arm64",
        "darwin-x64",
        "linux-arm64",
        "linux-x64",
        "linux-arm64-musl",
        "linux-x64-musl",
        "win32-arm64",
        "win32-x64",
    }
    assert len(PLATFORM_KEYS) == len(set(PLATFORM_KEYS))


@pytest.mark.parametrize("key", PLATFORM_KEYS)
def test_binary_filename_matches_platform_family(key):
    expected = "claude.exe" if key.startswith("win32-") else "claude"
    assert binary_filename(key) == expected


def test_binary_filename_rejects_unknown_key():
    with pytest.raises(PlatformError, match="Unknown platform key"):
        binary_filename("win32-x86")


def test_errors_derive_from_wrangler_error():
    assert issubclass(PlatformError, ClaudeWranglerError)
    assert issubclass(PayloadError, ClaudeWranglerError)


# --- Machine normalization and host detection ---------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("x86_64", "x64"),
        ("AMD64", "x64"),
        (" x86_64\n", "x64"),
        ("aarch64", "arm64"),
        ("ARM64", "arm64"),
        ("arm64", "arm64"),
        ("i686", "x86"),
        ("x86", "x86"),
        ("armv7l", None),
        ("riscv64", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_machine(raw, expected):
    assert normalize_machine(raw) == expected


@pytest.mark.parametrize(
    ("probes", "expected"),
    [
        (HostProbes("Darwin", "arm64"), "darwin-arm64"),
        (HostProbes("Darwin", "x86_64", rosetta_translated=True), "darwin-arm64"),
        (HostProbes("Darwin", "x86_64"), "darwin-x64"),
        (
            HostProbes("Windows", "AMD64", windows_native_machine="arm64"),
            "win32-arm64",
        ),
        (
            HostProbes("Windows", "x86", windows_native_machine="ARM64"),
            "win32-arm64",
        ),
        (HostProbes("Windows", "AMD64", windows_native_machine="x64"), "win32-x64"),
        (HostProbes("Windows", "x86", windows_native_machine="AMD64"), "win32-x64"),
        (HostProbes("Windows", "ARM64"), "win32-arm64"),
        (HostProbes("Windows", "AMD64"), "win32-x64"),
        (HostProbes("Linux", "x86_64"), "linux-x64"),
        (HostProbes("Linux", "x86_64", musl=True), "linux-x64-musl"),
        (HostProbes("Linux", "aarch64"), "linux-arm64"),
        (HostProbes("Linux", "aarch64", musl=True), "linux-arm64-musl"),
        (HostProbes("linux", "amd64"), "linux-x64"),
    ],
)
def test_host_platform_key_reflects_hardware(probes, expected):
    assert host_platform_key(probes) == expected
    assert expected in PLATFORM_KEYS


def test_musl_flag_is_ignored_outside_linux():
    assert host_platform_key(HostProbes("Darwin", "arm64", musl=True)) == (
        "darwin-arm64"
    )
    assert host_platform_key(HostProbes("Windows", "AMD64", musl=True)) == ("win32-x64")


@pytest.mark.parametrize(
    ("probes", "message"),
    [
        (HostProbes("Darwin", "i386"), "macOS architecture"),
        (HostProbes("Darwin", "ppc"), "macOS architecture"),
        (HostProbes("Windows", "x86", windows_native_machine="x86"), "'x86'"),
        (HostProbes("Windows", "AMD64", windows_native_machine="ia64"), "'ia64'"),
        (HostProbes("Windows", "x86"), "Windows architecture"),
        (HostProbes("Linux", "armv7l"), "Linux architecture"),
        (HostProbes("Linux", "riscv64", musl=True), "Linux architecture"),
        (HostProbes("FreeBSD", "amd64"), "operating system"),
        (HostProbes("CYGWIN_NT-10.0", "x86_64"), "operating system"),
    ],
)
def test_host_platform_key_rejects_unsupported_hosts(probes, message):
    with pytest.raises(PlatformError, match=message):
        host_platform_key(probes)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("1\n", True), ("0", False), ("", False), (None, False)],
)
def test_detect_rosetta_translated_uses_proc_translated(value, expected):
    requested = []

    def reader(name):
        """Record the requested sysctl name and return the scripted value."""

        requested.append(name)
        return value

    assert detect_rosetta_translated(reader) is expected
    assert requested == ["sysctl.proc_translated"]


class FakeRunner:
    """Record subprocess.run calls and replay a scripted result."""

    def __init__(self, returncode=0, stdout=b"", error=None):
        """Script the exit status, output, or exception to produce."""

        self.returncode = returncode
        self.stdout = stdout
        self.error = error
        self.calls = []

    def __call__(self, command, **kwargs):
        """Record one call, then raise or return the scripted result."""

        self.calls.append((command, kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(command, self.returncode, self.stdout)


def test_sysctl_command_reads_value():
    runner = FakeRunner(stdout=b"1\n")
    assert platforms._sysctl_via_command("sysctl.proc_translated", runner) == "1"
    command, kwargs = runner.calls[0]
    assert command == ["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"]
    assert kwargs["timeout"] > 0
    assert kwargs["check"] is False


@pytest.mark.parametrize(
    "runner",
    [
        FakeRunner(returncode=1, stdout=b"unknown oid"),
        FakeRunner(error=FileNotFoundError("sysctl")),
        FakeRunner(error=subprocess.TimeoutExpired("sysctl", 10)),
    ],
)
def test_sysctl_command_failures_mean_unknown(runner):
    assert platforms._sysctl_via_command("sysctl.proc_translated", runner) is None


def test_read_sysctl_falls_back_to_command_only_without_ctypes(monkeypatch):
    commands = []

    def fake_command(name):
        """Record a sysctl command fallback and report translation."""

        commands.append(name)
        return "1"

    monkeypatch.setattr(platforms, "_sysctl_via_command", fake_command)
    monkeypatch.setattr(platforms, "_sysctl_via_ctypes", lambda name: (False, None))
    assert platforms._read_sysctl("sysctl.proc_translated") == "1"
    assert commands == ["sysctl.proc_translated"]

    commands.clear()
    monkeypatch.setattr(platforms, "_sysctl_via_ctypes", lambda name: (True, None))
    assert platforms._read_sysctl("sysctl.proc_translated") is None
    assert commands == []


@pytest.mark.skipif(sys.platform == "darwin", reason="sysctlbyname exists on macOS")
def test_sysctl_ctypes_is_unavailable_off_macos():
    assert platforms._sysctl_via_ctypes("sysctl.proc_translated") == (False, None)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_live_rosetta_probe_matches_sysctl_on_macos():
    # `sysctl -n sysctl.proc_translated` prints 1 under Rosetta 2 and 0 on
    # native processes; on Intel Macs the name does not exist (exit 1).
    completed = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    expected = completed.returncode == 0 and completed.stdout.strip() == b"1"

    assert detect_rosetta_translated() is expected


@pytest.mark.parametrize(
    ("native", "environ", "expected"),
    [
        (0xAA64, {"PROCESSOR_ARCHITECTURE": "AMD64"}, "arm64"),
        (0x8664, {"PROCESSOR_ARCHITECTURE": "ARM64"}, "x64"),
        (0x014C, {}, "x86"),
        (
            None,
            {"PROCESSOR_ARCHITEW6432": "ARM64", "PROCESSOR_ARCHITECTURE": "x86"},
            "arm64",
        ),
        (
            None,
            {"PROCESSOR_ARCHITEW6432": "AMD64", "PROCESSOR_ARCHITECTURE": "x86"},
            "x64",
        ),
        (None, {"PROCESSOR_ARCHITECTURE": "AMD64"}, "x64"),
        (None, {"PROCESSOR_ARCHITECTURE": "ARM64"}, "arm64"),
        (0x1234, {"PROCESSOR_ARCHITECTURE": "AMD64"}, "x64"),
        (None, {"PROCESSOR_ARCHITECTURE": "IA64"}, "ia64"),
        (None, {"PROCESSOR_ARCHITEW6432": " ", "PROCESSOR_ARCHITECTURE": "x86"}, "x86"),
        (None, {}, None),
    ],
)
def test_detect_windows_native_machine(native, environ, expected):
    assert detect_windows_native_machine(lambda: native, environ) == expected


def test_windows_arm64_under_x64_emulation_selects_arm64_binary():
    native = detect_windows_native_machine(
        lambda: 0xAA64, {"PROCESSOR_ARCHITECTURE": "AMD64"}
    )
    probes = HostProbes("Windows", "AMD64", windows_native_machine=native)
    assert host_platform_key(probes) == "win32-arm64"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_live_is_wow64_process2_reports_a_64_bit_machine():
    assert platforms._is_wow64_process2_native_machine() in (0x8664, 0xAA64)


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows only")
def test_is_wow64_process2_is_skipped_off_windows():
    assert platforms._is_wow64_process2_native_machine() is None


def test_detect_musl_prefers_libc_file_without_running_ldd():
    def ldd():
        """Fail the test if the ldd fallback runs."""

        raise AssertionError("ldd must not run when a musl libc file exists")

    assert detect_musl(lambda: ["/lib/libc.musl-x86_64.so.1"], ldd) is True


@pytest.mark.parametrize(
    ("ldd_text", "expected"),
    [
        (
            "\t/lib/ld-musl-x86_64.so.1 (0x7f00)\n"
            "\tlibc.musl-x86_64.so.1 => /lib/ld-musl-x86_64.so.1 (0x7f00)\n",
            True,
        ),
        (
            "\tlinux-vdso.so.1 (0x7ffd)\n"
            "\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x7f00)\n",
            False,
        ),
        ("", False),
    ],
)
def test_detect_musl_falls_back_to_ldd_output(ldd_text, expected):
    assert detect_musl(lambda: [], lambda: ldd_text) is expected


def test_musl_libc_files_use_install_sh_glob_and_regular_files():
    patterns = []

    def fake_glob(pattern):
        """Record the glob pattern and return two candidate libc files."""

        patterns.append(pattern)
        return ["/lib/libc.musl-x86_64.so.1", "/lib/libc.musl-aarch64.so.1"]

    found = platforms._musl_libc_files(
        fake_glob, lambda path: path.endswith("aarch64.so.1")
    )
    assert patterns == ["/lib/libc.musl-*.so.1"]
    assert found == ["/lib/libc.musl-aarch64.so.1"]


def test_ldd_probe_runs_ldd_on_bin_ls_with_merged_output():
    runner = FakeRunner(returncode=1, stdout=b"musl libc (x86_64)\n")
    assert "musl" in platforms._ldd_ls_output(runner)
    command, kwargs = runner.calls[0]
    assert command == ["ldd", "/bin/ls"]
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["timeout"] > 0


@pytest.mark.parametrize(
    "runner",
    [
        FakeRunner(error=FileNotFoundError("ldd")),
        FakeRunner(error=subprocess.TimeoutExpired("ldd", 10)),
    ],
)
def test_ldd_probe_failures_return_empty_output(runner):
    assert platforms._ldd_ls_output(runner) == ""


@pytest.mark.parametrize(
    ("system", "probe", "field", "value"),
    [
        ("Darwin", "detect_rosetta_translated", "rosetta_translated", True),
        ("Windows", "detect_windows_native_machine", "windows_native_machine", "arm64"),
        ("Linux", "detect_musl", "musl", True),
    ],
)
def test_host_probes_detect_runs_only_the_relevant_probe(
    monkeypatch, system, probe, field, value
):
    called = []
    for name in (
        "detect_rosetta_translated",
        "detect_windows_native_machine",
        "detect_musl",
    ):
        result = value if name == probe else None
        monkeypatch.setattr(
            platforms,
            name,
            lambda name=name, result=result: called.append(name) or result,
        )
    monkeypatch.setattr(platforms.platform, "system", lambda: system)
    monkeypatch.setattr(platforms.platform, "machine", lambda: "x86_64")

    probes = HostProbes.detect()

    assert called == [probe]
    assert probes.system == system
    assert probes.machine == "x86_64"
    assert getattr(probes, field) == value


def test_host_probes_detect_skips_probes_on_other_systems(monkeypatch):
    for name in (
        "detect_rosetta_translated",
        "detect_windows_native_machine",
        "detect_musl",
    ):
        monkeypatch.setattr(platforms, name, lambda: pytest.fail("probe ran"))
    monkeypatch.setattr(platforms.platform, "system", lambda: "FreeBSD")
    monkeypatch.setattr(platforms.platform, "machine", lambda: "amd64")
    assert HostProbes.detect() == HostProbes("FreeBSD", "amd64")


def test_live_host_platform_key_is_a_manifest_key():
    if sys.platform not in ("win32", "darwin") and not sys.platform.startswith("linux"):
        pytest.skip("unsupported host operating system")
    assert host_platform_key() in PLATFORM_KEYS


# --- Platform override resolution ---------------------------------------


MANIFEST_KEYS = list(PLATFORM_KEYS)


def test_resolve_platform_key_accepts_manifest_override():
    assert resolve_platform_key("linux-x64", MANIFEST_KEYS) == "linux-x64"
    assert resolve_platform_key(" win32-arm64 ", MANIFEST_KEYS) == "win32-arm64"


def test_resolve_platform_key_detects_when_no_override():
    probes = HostProbes("Linux", "aarch64", musl=True)
    assert resolve_platform_key(None, MANIFEST_KEYS, probes) == "linux-arm64-musl"


@pytest.mark.parametrize("override", ["linux-riscv64", "", "x64", "WIN32-X64"])
def test_resolve_platform_key_rejects_unknown_override(override):
    with pytest.raises(PlatformError, match="not a supported platform key"):
        resolve_platform_key(override, MANIFEST_KEYS)


def test_resolve_platform_key_requires_manifest_membership():
    with pytest.raises(PlatformError, match="not published"):
        resolve_platform_key("win32-arm64", ["win32-x64", "linux-x64"])
    with pytest.raises(PlatformError, match="not published"):
        resolve_platform_key(None, ["win32-x64"], HostProbes("Darwin", "arm64"))


# --- Structural checks: PE ------------------------------------------------


def test_pe_x64_passes_for_win32_x64(tmp_path):
    path = write_binary(tmp_path, build_pe(PE_X64))
    check_executable_structure(path, "win32-x64")
    info = describe_executable(path)
    assert info.format == "PE"
    assert info.architectures == ("x64",)
    assert info.file_size == MIN_EXECUTABLE_SIZE


def test_pe_arm64_passes_for_win32_arm64(tmp_path):
    path = write_binary(tmp_path, build_pe(PE_ARM64))
    check_executable_structure(path, "win32-arm64")


@pytest.mark.parametrize(
    ("machine", "key", "message"),
    [
        (PE_X64, "win32-arm64", "targets x64, but win32-arm64 requires arm64"),
        (PE_ARM64, "win32-x64", "targets arm64, but win32-x64 requires x64"),
        (0x014C, "win32-x64", "targets x86"),
        (0x1234, "win32-x64", "machine-0x1234"),
    ],
)
def test_pe_machine_mismatch_is_rejected(tmp_path, machine, key, message):
    path = write_binary(tmp_path, build_pe(machine))
    with pytest.raises(PayloadError, match=message):
        check_executable_structure(path, key)


@pytest.mark.parametrize("key", ["linux-x64", "darwin-arm64"])
def test_pe_is_rejected_for_non_windows_keys(tmp_path, key):
    path = write_binary(tmp_path, build_pe())
    with pytest.raises(PayloadError, match="is in PE format"):
        check_executable_structure(path, key)


@pytest.mark.parametrize(
    ("offset", "fmt", "value", "message"),
    [
        (PE_OFFSET, "4s", b"PE\0\1", "PE signature"),
        (0x3C, "<I", 0x10, "overlaps the DOS header"),
        (0x3C, "<I", MIN_EXECUTABLE_SIZE - 8, "beyond"),
        (PE_CHARACTERISTICS, "<H", 0x2022, "DLL"),
        (PE_CHARACTERISTICS, "<H", 0x0020, "not marked executable"),
        (PE_SECTION_COUNT, "<H", 0, "declares 0 sections"),
        (PE_SECTION_COUNT, "<H", 97, "declares 97 sections"),
        (PE_OFFSET + 20, "<H", 64, "optional header size"),
        (PE_OPTIONAL, "<H", 0x10B, "32-bit"),
        (PE_OPTIONAL, "<H", 0x107, "optional header magic"),
        (PE_SUBSYSTEM, "<H", 2, "console subsystem"),
        (PE_OPTIONAL + 108, "<I", 100, "data directory"),
        (PE_OPTIONAL + 60, "<I", 0x100, "SizeOfHeaders"),
        (PE_SECTION_TABLE + 16, "<I", MIN_EXECUTABLE_SIZE, "PE section 0"),
        (PE_CERT_DIRECTORY, "<II", (MIN_EXECUTABLE_SIZE - 16, 64), "certificate"),
    ],
)
def test_pe_header_defects_are_rejected(tmp_path, offset, fmt, value, message):
    values = value if isinstance(value, tuple) else (value,)
    path = write_binary(tmp_path, patch(build_pe(), offset, fmt, *values))
    with pytest.raises(PayloadError, match=message):
        check_executable_structure(path, "win32-x64")


def test_pe_truncated_file_is_rejected(tmp_path):
    header = patch(build_pe(), PE_CERT_DIRECTORY, "<II", 0x10000, 0x1000)
    path = write_binary(tmp_path, header, size=0x10800)
    with pytest.raises(PayloadError, match="certificate table"):
        check_executable_structure(path, "win32-x64", min_size=0)


def test_pe_certificate_ending_at_eof_passes(tmp_path):
    header = patch(
        build_pe(), PE_CERT_DIRECTORY, "<II", MIN_EXECUTABLE_SIZE - 0x100, 0x100
    )
    check_executable_structure(write_binary(tmp_path, header), "win32-x64")


# --- Structural checks: sizes, file types, and unknown formats -----------


def test_small_file_is_rejected_by_minimum_size(tmp_path):
    path = write_binary(tmp_path, build_pe(), size=0x1000)
    with pytest.raises(PayloadError, match="below the 1048576-byte minimum"):
        check_executable_structure(path, "win32-x64")
    check_executable_structure(path, "win32-x64", min_size=0x1000)


def test_empty_file_is_rejected(tmp_path):
    path = tmp_path / "empty"
    path.write_bytes(b"")
    with pytest.raises(PayloadError, match="beyond the 0-byte file"):
        check_executable_structure(path, "win32-x64")


@pytest.mark.parametrize(
    "content",
    [
        b'#!/bin/sh\nexec node cli.js "$@"\n',
        b"<!DOCTYPE html><html><body>Access Denied</body></html>",
        b"\0\0\0\0",
    ],
)
def test_unknown_formats_are_rejected(tmp_path, content):
    path = write_binary(tmp_path, content)
    with pytest.raises(PayloadError, match="not a PE, Mach-O, or ELF"):
        check_executable_structure(path, "linux-x64")


def test_directory_is_rejected(tmp_path):
    with pytest.raises(PayloadError, match="not a regular file"):
        check_executable_structure(tmp_path, "linux-x64")


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(PayloadError, match="Cannot read"):
        check_executable_structure(tmp_path / "missing", "linux-x64")


def test_symlink_is_rejected(tmp_path):
    target = write_binary(tmp_path, build_pe(), name="real.exe")
    link = tmp_path / "link.exe"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable here")
    with pytest.raises(PayloadError, match="not a regular file"):
        check_executable_structure(link, "win32-x64")


def test_unknown_platform_key_is_a_platform_error(tmp_path):
    path = write_binary(tmp_path, build_pe())
    with pytest.raises(PlatformError, match="Unknown platform key"):
        check_executable_structure(path, "win32-ia64")


def test_structure_check_reads_only_headers(tmp_path, monkeypatch):
    size = 8 * MIN_EXECUTABLE_SIZE
    cases = [
        (build_pe(), "win32-x64"),
        (build_elf(), "linux-x64"),
        (build_macho(), "darwin-x64"),
        (build_fat([build_macho(CPU_X86_64), build_macho(CPU_ARM64)]), "darwin-arm64"),
    ]
    original_read = platforms._HeaderReader.read
    totals = []

    def counting_read(self, offset, length, label):
        """Count requested bytes before delegating to the real read."""

        totals[-1] += length
        return original_read(self, offset, length, label)

    monkeypatch.setattr(platforms._HeaderReader, "read", counting_read)
    for index, (header, key) in enumerate(cases):
        path = write_binary(tmp_path, header, size=size, name="case{}".format(index))
        totals.append(0)
        check_executable_structure(path, key, check_interpreter=False)
    assert len(totals) == len(cases)
    assert max(totals) < 4096


# --- Structural checks: ELF -----------------------------------------------


@pytest.mark.parametrize(
    ("machine", "interpreter", "key"),
    [
        (EM_X86_64, GLIBC_X64, "linux-x64"),
        (EM_AARCH64, GLIBC_ARM64, "linux-arm64"),
        (EM_X86_64, MUSL_X64, "linux-x64-musl"),
        (EM_AARCH64, MUSL_ARM64, "linux-arm64-musl"),
        (EM_X86_64, None, "linux-x64"),
        (EM_X86_64, None, "linux-x64-musl"),
        (EM_X86_64, "/opt/custom/loader.so", "linux-x64-musl"),
    ],
)
def test_elf_passes_for_matching_key(tmp_path, machine, interpreter, key):
    path = write_binary(tmp_path, build_elf(machine, interpreter))
    check_executable_structure(path, key, check_interpreter=False)
    info = describe_executable(path)
    assert info.format == "ELF"
    assert info.interpreter == interpreter
    assert elf_interpreter(path) == interpreter


@pytest.mark.parametrize(
    ("machine", "interpreter", "key", "message"),
    [
        (EM_X86_64, GLIBC_X64, "linux-arm64", "targets x64, but linux-arm64"),
        (EM_AARCH64, GLIBC_ARM64, "linux-x64", "targets arm64, but linux-x64"),
        (40, None, "linux-arm64", "targets arm"),
        (EM_X86_64, GLIBC_X64, "linux-x64-musl", "glibc interpreter"),
        (EM_AARCH64, MUSL_ARM64, "linux-arm64", "musl interpreter"),
        (EM_X86_64, GLIBC_X64, "win32-x64", "is in ELF format"),
        (EM_X86_64, GLIBC_X64, "darwin-x64", "is in ELF format"),
    ],
)
def test_elf_mismatch_is_rejected(tmp_path, machine, interpreter, key, message):
    path = write_binary(tmp_path, build_elf(machine, interpreter))
    with pytest.raises(PayloadError, match=message):
        check_executable_structure(path, key, check_interpreter=False)


@pytest.mark.parametrize(
    ("offset", "fmt", "value", "message"),
    [
        (4, "B", 1, "not 64-bit"),
        (5, "B", 2, "not little-endian"),
        (6, "B", 0, "identification version"),
        (16, "<H", 1, "not an executable"),
        (52, "<H", 32, "header size"),
        (54, "<H", 32, "program header entries are undersized"),
        (56, "<H", 0, "no program headers"),
        (32, "<Q", MIN_EXECUTABLE_SIZE, "program header table"),
        (40, "<Q", MIN_EXECUTABLE_SIZE - 16, "extended header counts|section header"),
        (64 + 56 + 8, "<Q", MIN_EXECUTABLE_SIZE - 0x10, "loadable segment 1"),
    ],
)
def test_elf_header_defects_are_rejected(tmp_path, offset, fmt, value, message):
    path = write_binary(tmp_path, patch(build_elf(), offset, fmt, value))
    with pytest.raises(PayloadError, match=message):
        check_executable_structure(path, "linux-x64", check_interpreter=False)


def test_elf_without_load_segment_is_rejected(tmp_path):
    path = write_binary(tmp_path, build_elf(load=None))
    with pytest.raises(PayloadError, match="no loadable segments"):
        check_executable_structure(path, "linux-x64", check_interpreter=False)


def test_elf_section_table_beyond_file_is_rejected(tmp_path):
    header = patch(build_elf(), 40, "<Q", MIN_EXECUTABLE_SIZE - 64)
    header = patch(header, 60, "<H", 4)
    path = write_binary(tmp_path, header)
    with pytest.raises(PayloadError, match="section header table"):
        check_executable_structure(path, "linux-x64", check_interpreter=False)


def test_elf_extended_program_header_count_is_read_from_section_zero(tmp_path):
    header = patch(build_elf(), 40, "<Q", 0x300)
    header = patch(header, 56, "<H", 0xFFFF)
    header = patch(header, 0x300 + 32, "<QII", 1, 0, 2)
    path = write_binary(tmp_path, header)
    check_executable_structure(path, "linux-x64", check_interpreter=False)
    assert elf_interpreter(path) == GLIBC_X64


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"/lib64/ld-linux-x86-64.so.2", "not NUL-terminated"),
        (b"ld-linux-x86-64.so.2\0", "not absolute"),
        (b"/lib/\xff\xfe\0", "not valid UTF-8"),
        (b"\0", "implausible size"),
    ],
)
def test_elf_bad_interpreter_is_rejected(tmp_path, raw, message):
    path = write_binary(tmp_path, build_elf(interpreter=raw))
    with pytest.raises(PayloadError, match=message):
        elf_interpreter(path)


def test_elf_interpreter_beyond_file_is_rejected(tmp_path):
    header = patch(build_elf(), 64 + 8, "<Q", MIN_EXECUTABLE_SIZE - 4)
    path = write_binary(tmp_path, header)
    with pytest.raises(PayloadError, match="interpreter path"):
        elf_interpreter(path)


def test_elf_interpreter_rejects_non_elf(tmp_path):
    path = write_binary(tmp_path, build_pe())
    with pytest.raises(PayloadError, match="not ELF"):
        elf_interpreter(path)


def test_elf_interpreter_must_exist_on_host_when_checked(tmp_path):
    path = write_binary(tmp_path, build_elf(interpreter=MUSL_X64))
    seen = []

    def missing(candidate):
        """Record the interpreter probed and report it absent."""

        seen.append(candidate)
        return False

    with pytest.raises(PayloadError, match="does not exist on this host"):
        check_executable_structure(
            path, "linux-x64-musl", check_interpreter=True, interpreter_exists=missing
        )
    assert seen == [MUSL_X64]

    check_executable_structure(
        path,
        "linux-x64-musl",
        check_interpreter=True,
        interpreter_exists=lambda candidate: True,
    )


def test_elf_interpreter_check_is_skipped_when_disabled(tmp_path):
    path = write_binary(tmp_path, build_elf())
    check_executable_structure(
        path,
        "linux-x64",
        check_interpreter=False,
        interpreter_exists=lambda candidate: pytest.fail("checked anyway"),
    )


def test_elf_interpreter_check_defaults_to_linux_hosts_only(tmp_path):
    path = write_binary(tmp_path, build_elf())
    seen = []

    def exists(candidate):
        """Record the interpreter probed and report it present."""

        seen.append(candidate)
        return True

    check_executable_structure(path, "linux-x64", interpreter_exists=exists)
    assert bool(seen) is sys.platform.startswith("linux")


def test_static_elf_skips_interpreter_check(tmp_path):
    path = write_binary(tmp_path, build_elf(interpreter=None))
    check_executable_structure(
        path,
        "linux-x64",
        check_interpreter=True,
        interpreter_exists=lambda candidate: pytest.fail("no interpreter to check"),
    )


# --- Structural checks: Mach-O ---------------------------------------------


@pytest.mark.parametrize(
    ("cpu_type", "key"),
    [(CPU_X86_64, "darwin-x64"), (CPU_ARM64, "darwin-arm64")],
)
def test_thin_macho_passes_for_matching_key(tmp_path, cpu_type, key):
    path = write_binary(tmp_path, build_macho(cpu_type))
    check_executable_structure(path, key)
    info = describe_executable(path)
    assert info.format == "Mach-O"
    assert info.universal is False


def test_thin_macho_with_code_signature_passes(tmp_path):
    header = build_macho(CPU_ARM64, code_signature=(MIN_EXECUTABLE_SIZE - 64, 64))
    check_executable_structure(write_binary(tmp_path, header), "darwin-arm64")


@pytest.mark.parametrize(
    ("cpu_type", "key", "message"),
    [
        (CPU_X86_64, "darwin-arm64", "targets x64, but darwin-arm64 requires arm64"),
        (CPU_ARM64, "darwin-x64", "targets arm64, but darwin-x64 requires x64"),
        (CPU_X86_64, "linux-x64", "is in Mach-O format"),
        (CPU_ARM64, "win32-arm64", "is in Mach-O format"),
    ],
)
def test_thin_macho_mismatch_is_rejected(tmp_path, cpu_type, key, message):
    path = write_binary(tmp_path, build_macho(cpu_type))
    with pytest.raises(PayloadError, match=message):
        check_executable_structure(path, key)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"magic": b"\xce\xfa\xed\xfe"}, "32-bit Mach-O"),
        ({"file_type": 6}, "not an executable"),
        ({"entry": False}, "no entry point"),
        ({"segment": None}, "no segments"),
        ({"segment": (MIN_EXECUTABLE_SIZE - 16, 64)}, "segment 0 ends"),
        ({"code_signature": (MIN_EXECUTABLE_SIZE - 16, 64)}, "code signature"),
    ],
)
def test_thin_macho_defects_are_rejected(tmp_path, kwargs, message):
    path = write_binary(tmp_path, build_macho(**kwargs))
    with pytest.raises(PayloadError, match=message):
        check_executable_structure(path, "darwin-x64")


@pytest.mark.parametrize(
    ("offset", "fmt", "value", "message"),
    [
        (16, "<I", 0, "invalid load command counts"),
        (16, "<I", 100, "invalid load command counts"),
        (20, "<I", 2 * 1024 * 1024, "implausibly large"),
        (32 + 4, "<I", 76, "invalid size 76"),
        (32 + 4, "<I", 4096, "invalid size 4096"),
        (32 + 64, "<I", 3, "too small for its sections"),
    ],
)
def test_thin_macho_load_command_defects_are_rejected(
    tmp_path, offset, fmt, value, message
):
    path = write_binary(tmp_path, patch(build_macho(), offset, fmt, value))
    with pytest.raises(PayloadError, match=message):
        check_executable_structure(path, "darwin-x64")


def test_thin_macho_load_commands_beyond_small_file_are_rejected(tmp_path):
    header = patch(build_macho(segment=(0, 0x100)), 20, "<I", 0x2000)
    path = write_binary(tmp_path, header, size=0x1000)
    with pytest.raises(PayloadError, match="overrun the image"):
        check_executable_structure(path, "darwin-x64", min_size=0)


@pytest.mark.parametrize("fat64", [False, True])
def test_universal_macho_passes_for_each_carried_architecture(tmp_path, fat64):
    data = build_fat([build_macho(CPU_X86_64), build_macho(CPU_ARM64)], fat64=fat64)
    path = write_binary(tmp_path, data)
    check_executable_structure(path, "darwin-x64")
    check_executable_structure(path, "darwin-arm64")
    info = describe_executable(path)
    assert info.universal is True
    assert info.architectures == ("x64", "arm64")


def test_universal_macho_without_expected_slice_is_rejected(tmp_path):
    path = write_binary(tmp_path, build_fat([build_macho(CPU_X86_64)]))
    with pytest.raises(PayloadError, match="targets x64, but darwin-arm64"):
        check_executable_structure(path, "darwin-arm64")


@pytest.mark.parametrize(
    ("count", "message"),
    [(0, "declares 0 architectures"), (52, "declares 52 architectures")],
)
def test_universal_macho_bad_counts_are_rejected(tmp_path, count, message):
    # A count of 52 is how a Java 8 class file's version field reads.
    data = patch(build_fat([build_macho()]), 4, ">I", count)
    path = write_binary(tmp_path, data)
    with pytest.raises(PayloadError, match=message):
        check_executable_structure(path, "darwin-x64")


def test_universal_macho_slice_beyond_file_is_rejected(tmp_path):
    data = patch(build_fat([build_macho()]), 8 + 8, ">I", MIN_EXECUTABLE_SIZE - 0x100)
    path = write_binary(tmp_path, data)
    with pytest.raises(PayloadError, match="slice 0"):
        check_executable_structure(path, "darwin-x64")


def test_universal_macho_empty_slice_is_rejected(tmp_path):
    data = patch(build_fat([build_macho()]), 8 + 12, ">I", 0)
    path = write_binary(tmp_path, data)
    with pytest.raises(PayloadError, match="is empty"):
        check_executable_structure(path, "darwin-x64")


def test_universal_macho_cpu_disagreement_is_rejected(tmp_path):
    data = build_fat([build_macho(CPU_X86_64)], table_cpu_types=[CPU_ARM64])
    path = write_binary(tmp_path, data)
    with pytest.raises(PayloadError, match="disagrees with its table entry"):
        check_executable_structure(path, "darwin-arm64")


def test_universal_macho_with_32_bit_slice_is_rejected(tmp_path):
    data = build_fat([build_macho(7, magic=b"\xce\xfa\xed\xfe")], table_cpu_types=[7])
    path = write_binary(tmp_path, data)
    with pytest.raises(PayloadError, match="32-bit Mach-O"):
        check_executable_structure(path, "darwin-x64")


def test_universal_macho_slice_segment_beyond_slice_is_rejected(tmp_path):
    image = build_macho(CPU_ARM64, segment=(0, 0x10000))
    path = write_binary(tmp_path, build_fat([image], slice_size=0x8000))
    with pytest.raises(PayloadError, match="beyond the 32768-byte image"):
        check_executable_structure(path, "darwin-arm64")


# --- Optional real-binary check ---------------------------------------------


def test_real_claude_binary_passes_structure_check():
    raw = os.environ.get("CLAUDE_WRANGLER_TEST_REAL_BINARY")
    if not raw:
        pytest.skip("set CLAUDE_WRANGLER_TEST_REAL_BINARY to check a real binary")
    key = os.environ.get("CLAUDE_WRANGLER_TEST_REAL_BINARY_PLATFORM") or (
        host_platform_key()
    )
    path = Path(raw).resolve()
    check_executable_structure(path, key)
    assert describe_executable(path).file_size > MIN_EXECUTABLE_SIZE
