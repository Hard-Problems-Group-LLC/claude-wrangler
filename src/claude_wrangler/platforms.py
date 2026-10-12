"""Host platform detection and executable structure checks.

claude-wrangler installs Anthropic's self-contained native Claude Code
binary, published per platform under keys such as ``linux-x64-musl`` or
``win32-arm64``. This module answers two questions for that flow:

* Which platform key matches this host's hardware? Detection must reflect
  the machine rather than the Python process, so an x64 Python under
  Rosetta 2 or under Windows ARM64 x64 emulation still selects the native
  arm64 binary, and Linux hosts select the ``-musl`` build using the same
  file checks as Anthropic's ``install.sh``. Every host fact is gathered
  into an injectable ``HostProbes`` value so tests can cover every branch
  on any operating system.
* Does a downloaded or migrated file have the executable format and CPU
  type that its platform key promises? The structural check parses only
  bounded header regions of PE, Mach-O (thin and universal), and ELF
  files, confirms that every region those headers declare lies inside the
  file, and, for ELF, confirms that the program interpreter exists on the
  host. It never reads the whole binary.

The module is standard-library-only and Python 3.9 compatible because it
runs on the bootstrap path, and it never executes the file it inspects.
"""

from __future__ import annotations

from dataclasses import dataclass
import glob
import os
from pathlib import Path
import platform
import stat
import struct
import subprocess
import sys
from typing import (
    BinaryIO,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from .models import ClaudeWranglerError

PLATFORM_KEYS: Tuple[str, ...] = (
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64",
    "linux-x64",
    "linux-arm64-musl",
    "linux-x64-musl",
    "win32-arm64",
    "win32-x64",
)
"""Platform keys published in Anthropic's release manifests."""

MIN_EXECUTABLE_SIZE = 1024 * 1024
"""Smallest file accepted as a native Claude Code binary.

Real releases are hundreds of megabytes; anything under one mebibyte is an
error page, a wrapper script, or a stub, never the self-contained binary.
"""

FORMAT_PE = "PE"
FORMAT_MACHO = "Mach-O"
FORMAT_ELF = "ELF"

# Expected executable format and CPU architecture for each platform key.
_PLATFORM_EXPECTATIONS: Dict[str, Tuple[str, str]] = {
    "darwin-arm64": (FORMAT_MACHO, "arm64"),
    "darwin-x64": (FORMAT_MACHO, "x64"),
    "linux-arm64": (FORMAT_ELF, "arm64"),
    "linux-x64": (FORMAT_ELF, "x64"),
    "linux-arm64-musl": (FORMAT_ELF, "arm64"),
    "linux-x64-musl": (FORMAT_ELF, "x64"),
    "win32-arm64": (FORMAT_PE, "arm64"),
    "win32-x64": (FORMAT_PE, "x64"),
}

_MACHINE_ALIASES: Dict[str, str] = {
    "x86_64": "x64",
    "x86-64": "x64",
    "amd64": "x64",
    "x64": "x64",
    "em64t": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "i386": "x86",
    "i486": "x86",
    "i586": "x86",
    "i686": "x86",
    "x86": "x86",
}

# PE/COFF machine values, also returned by IsWow64Process2.
_IMAGE_FILE_MACHINE_I386 = 0x014C
_IMAGE_FILE_MACHINE_ARMNT = 0x01C4
_IMAGE_FILE_MACHINE_AMD64 = 0x8664
_IMAGE_FILE_MACHINE_ARM64 = 0xAA64
_PE_MACHINE_NAMES: Dict[int, str] = {
    _IMAGE_FILE_MACHINE_I386: "x86",
    _IMAGE_FILE_MACHINE_ARMNT: "arm",
    _IMAGE_FILE_MACHINE_AMD64: "x64",
    _IMAGE_FILE_MACHINE_ARM64: "arm64",
}
_IMAGE_FILE_EXECUTABLE_IMAGE = 0x0002
_IMAGE_FILE_DLL = 0x2000
_PE32_PLUS_MAGIC = 0x020B
_PE32_MAGIC = 0x010B
_IMAGE_SUBSYSTEM_WINDOWS_CUI = 3
_PE_MAX_SECTIONS = 96
_PE_MAX_OPTIONAL_HEADER = 0x1000
_PE_CERTIFICATE_DIRECTORY = 4

# Mach-O constants (mach-o/loader.h and mach-o/fat.h).
_CPU_ARCH_ABI64 = 0x01000000
_CPU_TYPE_X86 = 7
_CPU_TYPE_ARM = 12
_CPU_TYPE_X86_64 = _CPU_ARCH_ABI64 | _CPU_TYPE_X86
_CPU_TYPE_ARM64 = _CPU_ARCH_ABI64 | _CPU_TYPE_ARM
_MACHO_CPU_NAMES: Dict[int, str] = {
    _CPU_TYPE_X86: "x86",
    _CPU_TYPE_ARM: "arm",
    _CPU_TYPE_X86_64: "x64",
    _CPU_TYPE_ARM64: "arm64",
}
_MACHO64_MAGICS: Dict[bytes, str] = {
    b"\xcf\xfa\xed\xfe": "<",
    b"\xfe\xed\xfa\xcf": ">",
}
_MACHO32_MAGICS = (b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce")
_FAT_MAGIC = b"\xca\xfe\xba\xbe"
_FAT_MAGIC_64 = b"\xca\xfe\xba\xbf"
_MH_EXECUTE = 2
_LC_REQ_DYLD = 0x80000000
_LC_UNIXTHREAD = 0x5
_LC_SEGMENT_64 = 0x19
_LC_CODE_SIGNATURE = 0x1D
_LC_MAIN = 0x28 | _LC_REQ_DYLD
# Java class files share the 0xCAFEBABE magic; their version field reads as
# an architecture count of 45 or more, so a small cap tells them apart.
_FAT_MAX_ARCHITECTURES = 30
_MACHO_MAX_LOAD_COMMAND_BYTES = 1024 * 1024

# ELF constants (elf.h).
_ELFCLASS64 = 2
_ELFDATA2LSB = 1
_EV_CURRENT = 1
_ET_EXEC = 2
_ET_DYN = 3
_EM_386 = 3
_EM_ARM = 40
_EM_X86_64 = 62
_EM_AARCH64 = 183
_ELF_MACHINE_NAMES: Dict[int, str] = {
    _EM_386: "x86",
    _EM_ARM: "arm",
    _EM_X86_64: "x64",
    _EM_AARCH64: "arm64",
}
_PT_LOAD = 1
_PT_INTERP = 3
_PN_XNUM = 0xFFFF
_ELF64_HEADER_SIZE = 64
_ELF64_PHDR_SIZE = 56
_ELF64_SHDR_SIZE = 64
_ELF_MAX_PROGRAM_HEADER_BYTES = 1024 * 1024
_ELF_MAX_INTERPRETER_BYTES = 4096

_MUSL_LIBC_GLOB = "/lib/libc.musl-*.so.1"
_SYSCTL_PATH = "/usr/sbin/sysctl"
_PROBE_TIMEOUT_SECONDS = 10.0


class PlatformError(ClaudeWranglerError):
    """Raised when a platform key cannot be detected, parsed, or accepted."""


class PayloadError(ClaudeWranglerError):
    """Raised when a file is not a structurally valid binary for its key."""


@dataclass
class HostProbes:
    """Host facts that decide the platform key, injectable for tests.

    ``system`` and ``machine`` use ``platform.system()`` and
    ``platform.machine()`` spellings. ``rosetta_translated`` is meaningful
    only on macOS, ``windows_native_machine`` (an architecture name such as
    ``arm64`` or ``AMD64``, or ``None`` when unknown) only on Windows, and
    ``musl`` only on Linux.
    """

    system: str
    machine: str
    rosetta_translated: bool = False
    windows_native_machine: Optional[str] = None
    musl: bool = False

    @classmethod
    def detect(cls) -> "HostProbes":
        """Gather the live facts for this host, probing only what matters."""

        probes = cls(system=platform.system(), machine=platform.machine())
        system = probes.system.strip().lower()
        if system == "darwin":
            probes.rosetta_translated = detect_rosetta_translated()
        elif system == "windows":
            probes.windows_native_machine = detect_windows_native_machine()
        elif system == "linux":
            probes.musl = detect_musl()
        return probes


@dataclass(frozen=True)
class ExecutableInfo:
    """Structural summary of one native executable file.

    ``architectures`` lists every CPU architecture the file carries, in
    file order (several only for a universal Mach-O). Unknown machine
    values are spelled ``machine-0x<hex>``. ``interpreter`` is the ELF
    ``PT_INTERP`` path, or ``None`` for other formats and static ELF files.
    """

    format: str
    architectures: Tuple[str, ...]
    file_size: int
    universal: bool = False
    interpreter: Optional[str] = None


def normalize_machine(raw: Optional[str]) -> Optional[str]:
    """Map an OS machine spelling to ``x64``, ``arm64``, ``x86``, or None."""

    if raw is None:
        return None
    return _MACHINE_ALIASES.get(raw.strip().lower())


def _sysctl_via_ctypes(name: str) -> Tuple[bool, Optional[str]]:
    """Read an integer sysctl through ``sysctlbyname``.

    Returns ``(attempted, value)``. ``attempted`` is False when ctypes or
    the symbol is unavailable, so the caller can fall back to the sysctl
    command; a failed call (for example ENOENT on Intel Macs, which lack
    ``sysctl.proc_translated``) is a definitive ``(True, None)``.
    """

    try:
        import ctypes
        import ctypes.util

        library_path = ctypes.util.find_library("c")
        libc = ctypes.CDLL(library_path, use_errno=True)
        sysctlbyname = libc.sysctlbyname
    except (ImportError, OSError, AttributeError, TypeError, ValueError):
        return (False, None)

    value = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(value))
    try:
        result = sysctlbyname(
            name.encode("ascii"),
            ctypes.byref(value),
            ctypes.byref(size),
            None,
            ctypes.c_size_t(0),
        )
    except (OSError, ctypes.ArgumentError, ValueError):
        return (False, None)
    if result != 0:
        return (True, None)
    return (True, str(value.value))


def _sysctl_via_command(
    name: str,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> Optional[str]:
    """Read a sysctl through ``/usr/sbin/sysctl -n``; None on any failure."""

    try:
        completed = runner(
            [_SYSCTL_PATH, "-n", name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("ascii", "replace").strip()


def _read_sysctl(name: str) -> Optional[str]:
    """Read a sysctl value, preferring ctypes over spawning a process."""

    attempted, value = _sysctl_via_ctypes(name)
    if attempted:
        return value
    return _sysctl_via_command(name)


def detect_rosetta_translated(
    sysctl_reader: Optional[Callable[[str], Optional[str]]] = None,
) -> bool:
    """Return True when this process runs under Rosetta 2 on Apple silicon."""

    reader = _read_sysctl if sysctl_reader is None else sysctl_reader
    value = reader("sysctl.proc_translated")
    return value is not None and value.strip() == "1"


def _is_wow64_process2_native_machine() -> Optional[int]:
    """Return the native machine from ``IsWow64Process2``, or None.

    Unlike the process's own architecture, the native machine stays ARM64
    for an x64 process running under emulation on Windows on ARM.
    """

    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        # A private WinDLL instance keeps these prototypes from leaking into
        # ctypes.windll.kernel32, which other code may configure differently.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        is_wow64_process2 = kernel32.IsWow64Process2
        get_current_process = kernel32.GetCurrentProcess
    except (ImportError, OSError, AttributeError):
        return None

    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    is_wow64_process2.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.USHORT),
        ctypes.POINTER(wintypes.USHORT),
    ]
    is_wow64_process2.restype = wintypes.BOOL
    process_machine = wintypes.USHORT(0)
    native_machine = wintypes.USHORT(0)
    try:
        succeeded = is_wow64_process2(
            get_current_process(),
            ctypes.byref(process_machine),
            ctypes.byref(native_machine),
        )
    except (OSError, ctypes.ArgumentError):
        return None
    if not succeeded:
        return None
    return int(native_machine.value)


def detect_windows_native_machine(
    native_machine_reader: Optional[Callable[[], Optional[int]]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Return the Windows host's native architecture name, or None.

    ``IsWow64Process2`` is authoritative. When it is unavailable or reports
    an unrecognized machine, ``PROCESSOR_ARCHITEW6432`` (set for WOW64
    processes to the native architecture) and then
    ``PROCESSOR_ARCHITECTURE`` are consulted.
    """

    reader = (
        _is_wow64_process2_native_machine
        if native_machine_reader is None
        else native_machine_reader
    )
    native = reader()
    if native is not None and native in _PE_MACHINE_NAMES:
        return _PE_MACHINE_NAMES[native]

    env = os.environ if environ is None else environ
    for variable in ("PROCESSOR_ARCHITEW6432", "PROCESSOR_ARCHITECTURE"):
        value = env.get(variable, "").strip()
        if value:
            return normalize_machine(value) or value.lower()
    return None


def _musl_libc_files(
    glob_fn: Callable[[str], List[str]] = glob.glob,
    isfile: Callable[[str], bool] = os.path.isfile,
) -> List[str]:
    """Return Alpine-style ``/lib/libc.musl-*.so.1`` files that exist."""

    return [path for path in glob_fn(_MUSL_LIBC_GLOB) if isfile(path)]


def _ldd_ls_output(
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> str:
    """Return the combined output of ``ldd /bin/ls``, or "" on failure."""

    try:
        completed = runner(
            ["ldd", "/bin/ls"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.decode("utf-8", "replace")


def detect_musl(
    musl_libc_files: Optional[Callable[[], Sequence[str]]] = None,
    ldd_output: Optional[Callable[[], str]] = None,
) -> bool:
    """Return True when the Linux system C library is musl.

    Mirrors Anthropic's ``install.sh``: a ``/lib/libc.musl-<arch>.so.1``
    file, or ``ldd /bin/ls`` output mentioning musl. ``ldd`` runs only
    when no such file exists. ``platform.libc_ver()`` is deliberately not
    used because it reports nothing useful on musl systems.
    """

    finder = _musl_libc_files if musl_libc_files is None else musl_libc_files
    if finder():
        return True
    reader = _ldd_ls_output if ldd_output is None else ldd_output
    return "musl" in reader()


def host_platform_key(probes: Optional[HostProbes] = None) -> str:
    """Return the manifest platform key matching this host's hardware."""

    facts = HostProbes.detect() if probes is None else probes
    system = facts.system.strip().lower()
    machine = normalize_machine(facts.machine)

    if system == "darwin":
        if facts.rosetta_translated or machine == "arm64":
            return "darwin-arm64"
        if machine == "x64":
            return "darwin-x64"
        raise PlatformError(
            "Unsupported macOS architecture {!r}; Claude Code ships only "
            "arm64 and x64 binaries.".format(facts.machine)
        )

    if system == "windows":
        # The process architecture (facts.machine) is only a fallback: an
        # x64 Python emulated on Windows on ARM reports AMD64 there.
        raw_native = facts.windows_native_machine or facts.machine
        native = normalize_machine(raw_native)
        if native == "arm64":
            return "win32-arm64"
        if native == "x64":
            return "win32-x64"
        raise PlatformError(
            "Unsupported Windows architecture {!r}; Claude Code ships only "
            "64-bit x64 and arm64 binaries. Pass --platform to override "
            "detection.".format(raw_native)
        )

    if system == "linux":
        if machine not in ("x64", "arm64"):
            raise PlatformError(
                "Unsupported Linux architecture {!r}; Claude Code ships only "
                "x64 and arm64 binaries.".format(facts.machine)
            )
        suffix = "-musl" if facts.musl else ""
        return "linux-{}{}".format(machine, suffix)

    raise PlatformError(
        "Unsupported operating system {!r}; claude-wrangler supports "
        "Windows, macOS, and Linux. Pass --platform to override "
        "detection.".format(facts.system)
    )


def resolve_platform_key(
    override: Optional[str] = None,
    available_keys: Optional[Sequence[str]] = None,
    probes: Optional[HostProbes] = None,
) -> str:
    """Return the platform key to install, honoring a ``--platform`` override.

    The result must be a known platform key and, when ``available_keys``
    (the verified manifest's platform keys) is given, one of those too.
    """

    if override is not None:
        key = override.strip()
        source = "--platform {!r}".format(override)
    else:
        key = host_platform_key(probes)
        source = "Detected platform {!r}".format(key)

    if key not in _PLATFORM_EXPECTATIONS:
        raise PlatformError(
            "{} is not a supported platform key; choose one of: {}.".format(
                source, ", ".join(PLATFORM_KEYS)
            )
        )
    if available_keys is not None and key not in available_keys:
        raise PlatformError(
            "{} is not published in this release's manifest; it offers: "
            "{}.".format(source, ", ".join(sorted(available_keys)) or "nothing")
        )
    return key


def _require_known_key(platform_key: str) -> Tuple[str, str]:
    """Return the expected (format, architecture) for a platform key."""

    expectation = _PLATFORM_EXPECTATIONS.get(platform_key)
    if expectation is None:
        raise PlatformError(
            "Unknown platform key {!r}; expected one of: {}.".format(
                platform_key, ", ".join(PLATFORM_KEYS)
            )
        )
    return expectation


def binary_filename(platform_key: str) -> str:
    """Return the binary's file name for a platform key."""

    expected_format, _ = _require_known_key(platform_key)
    return "claude.exe" if expected_format == FORMAT_PE else "claude"


class _HeaderReader:
    """Bounded, offset-checked reads from one open executable file."""

    def __init__(self, handle: BinaryIO, path: Path, file_size: int) -> None:
        """Wrap an open binary handle whose size was taken from ``fstat``."""

        self.handle = handle
        self.path = path
        self.file_size = file_size

    def fail(self, message: str) -> PayloadError:
        """Return a PayloadError that names the inspected file."""

        return PayloadError("Executable {}: {}".format(self.path, message))

    def require_extent(self, offset: int, length: int, label: str) -> int:
        """Return the region end, rejecting a region that leaves the file."""

        end = offset + length
        if offset < 0 or length < 0 or end > self.file_size:
            raise self.fail(
                "{} spans bytes {}..{}, beyond the {}-byte file (truncated or "
                "corrupt).".format(label, offset, end, self.file_size)
            )
        return end

    def read(self, offset: int, length: int, label: str) -> bytes:
        """Read exactly ``length`` bytes at ``offset`` inside the file."""

        self.require_extent(offset, length, label)
        self.handle.seek(offset)
        data = self.handle.read(length)
        if len(data) != length:
            raise self.fail("changed size while reading {}.".format(label))
        return data


def _machine_name(names: Mapping[int, str], value: int) -> str:
    """Return the architecture name for a machine value, or a hex spelling."""

    return names.get(value, "machine-0x{:x}".format(value))


def _parse_pe(reader: _HeaderReader) -> ExecutableInfo:
    """Parse a PE32+ image header and check its declared regions."""

    dos_header = reader.read(0, 64, "DOS header")
    pe_offset = struct.unpack_from("<I", dos_header, 0x3C)[0]
    if pe_offset < 64:
        raise reader.fail(
            "PE header offset {} overlaps the DOS header.".format(pe_offset)
        )
    coff = reader.read(pe_offset, 24, "PE signature and COFF header")
    if coff[:4] != b"PE\0\0":
        raise reader.fail("missing the PE signature (not a Windows executable).")
    machine, section_count = struct.unpack_from("<HH", coff, 4)
    optional_size, characteristics = struct.unpack_from("<HH", coff, 20)
    if not characteristics & _IMAGE_FILE_EXECUTABLE_IMAGE:
        raise reader.fail("PE image is not marked executable.")
    if characteristics & _IMAGE_FILE_DLL:
        raise reader.fail("PE image is a DLL, not a program.")
    if section_count == 0 or section_count > _PE_MAX_SECTIONS:
        raise reader.fail("PE image declares {} sections.".format(section_count))
    if optional_size < 112 or optional_size > _PE_MAX_OPTIONAL_HEADER:
        raise reader.fail(
            "PE optional header size {} is implausible.".format(optional_size)
        )

    optional_offset = pe_offset + 24
    optional = reader.read(optional_offset, optional_size, "PE optional header")
    magic = struct.unpack_from("<H", optional, 0)[0]
    if magic == _PE32_MAGIC:
        raise reader.fail("PE image is 32-bit (PE32); a 64-bit PE32+ is required.")
    if magic != _PE32_PLUS_MAGIC:
        raise reader.fail("unknown PE optional header magic 0x{:x}.".format(magic))
    subsystem = struct.unpack_from("<H", optional, 68)[0]
    if subsystem != _IMAGE_SUBSYSTEM_WINDOWS_CUI:
        raise reader.fail(
            "PE subsystem {} is not the Windows console subsystem.".format(subsystem)
        )
    size_of_headers = struct.unpack_from("<I", optional, 60)[0]
    directory_count = struct.unpack_from("<I", optional, 108)[0]
    if 112 + directory_count * 8 > optional_size:
        raise reader.fail("PE data directory table overruns its optional header.")

    section_table_offset = optional_offset + optional_size
    section_table_end = reader.require_extent(
        section_table_offset, section_count * 40, "PE section table"
    )
    if size_of_headers < section_table_end:
        raise reader.fail("PE SizeOfHeaders ends before the section table.")
    reader.require_extent(0, size_of_headers, "PE headers")
    sections = reader.read(section_table_offset, section_count * 40, "PE section table")
    for index in range(section_count):
        raw_size, raw_offset = struct.unpack_from("<II", sections, index * 40 + 16)
        if raw_size:
            reader.require_extent(raw_offset, raw_size, "PE section {}".format(index))

    if directory_count > _PE_CERTIFICATE_DIRECTORY:
        # The certificate table directory holds a file offset, not an RVA,
        # and a signed image ends with it, so it catches tail truncation.
        cert_offset, cert_size = struct.unpack_from(
            "<II", optional, 112 + _PE_CERTIFICATE_DIRECTORY * 8
        )
        if cert_size:
            reader.require_extent(cert_offset, cert_size, "PE certificate table")

    return ExecutableInfo(
        format=FORMAT_PE,
        architectures=(_machine_name(_PE_MACHINE_NAMES, machine),),
        file_size=reader.file_size,
    )


def _parse_macho_slice(
    reader: _HeaderReader,
    slice_offset: int,
    slice_size: int,
    label: str,
) -> int:
    """Validate one thin 64-bit Mach-O image and return its CPU type."""

    reader.require_extent(slice_offset, slice_size, label)
    header = reader.read(slice_offset, 32, "{} header".format(label))
    if header[:4] in _MACHO32_MAGICS:
        raise reader.fail("{} is a 32-bit Mach-O image.".format(label))
    byte_order = _MACHO64_MAGICS.get(header[:4])
    if byte_order is None:
        raise reader.fail("{} is not a 64-bit Mach-O image.".format(label))
    cpu_type, _, file_type, command_count, command_bytes = struct.unpack_from(
        byte_order + "iiIII", header, 4
    )
    if file_type != _MH_EXECUTE:
        raise reader.fail(
            "{} has Mach-O file type {}, not an executable.".format(label, file_type)
        )
    if command_bytes > _MACHO_MAX_LOAD_COMMAND_BYTES:
        raise reader.fail("{} declares implausibly large load commands.".format(label))
    if command_count == 0 or command_count > command_bytes // 8:
        raise reader.fail("{} has invalid load command counts.".format(label))
    if 32 + command_bytes > slice_size:
        raise reader.fail("{} load commands overrun the image.".format(label))
    commands = reader.read(
        slice_offset + 32, command_bytes, "{} load commands".format(label)
    )

    position = 0
    segment_count = 0
    has_entry_point = False
    for index in range(command_count):
        if position + 8 > command_bytes:
            raise reader.fail(
                "{} load command {} overruns the table.".format(label, index)
            )
        command, command_size = struct.unpack_from(
            byte_order + "II", commands, position
        )
        if (
            command_size < 8
            or command_size % 8
            or position + command_size > command_bytes
        ):
            raise reader.fail(
                "{} load command {} has invalid size {}.".format(
                    label, index, command_size
                )
            )
        if command == _LC_SEGMENT_64:
            if command_size < 72:
                raise reader.fail("{} has an undersized segment command.".format(label))
            file_offset, file_size = struct.unpack_from(
                byte_order + "QQ", commands, position + 40
            )
            section_count = struct.unpack_from(
                byte_order + "I", commands, position + 64
            )[0]
            if command_size < 72 + section_count * 80:
                raise reader.fail(
                    "{} segment command is too small for its sections.".format(label)
                )
            if file_size and file_offset + file_size > slice_size:
                raise reader.fail(
                    "{} segment {} ends at slice byte {}, beyond the {}-byte "
                    "image (truncated or corrupt).".format(
                        label, index, file_offset + file_size, slice_size
                    )
                )
            segment_count += 1
        elif command == _LC_CODE_SIGNATURE:
            if command_size < 16:
                raise reader.fail(
                    "{} has an undersized code signature command.".format(label)
                )
            data_offset, data_size = struct.unpack_from(
                byte_order + "II", commands, position + 8
            )
            if data_size and data_offset + data_size > slice_size:
                raise reader.fail(
                    "{} code signature lies beyond the image (truncated or "
                    "corrupt).".format(label)
                )
        elif command in (_LC_MAIN, _LC_UNIXTHREAD):
            has_entry_point = True
        position += command_size

    if segment_count == 0:
        raise reader.fail("{} has no segments.".format(label))
    if not has_entry_point:
        raise reader.fail("{} has no entry point command.".format(label))
    return cpu_type


def _parse_macho(reader: _HeaderReader, magic: bytes) -> ExecutableInfo:
    """Parse a thin or universal Mach-O file of 64-bit images."""

    if magic in _MACHO64_MAGICS or magic in _MACHO32_MAGICS:
        cpu_type = _parse_macho_slice(reader, 0, reader.file_size, "Mach-O image")
        return ExecutableInfo(
            format=FORMAT_MACHO,
            architectures=(_machine_name(_MACHO_CPU_NAMES, cpu_type),),
            file_size=reader.file_size,
        )

    is_fat64 = magic == _FAT_MAGIC_64
    entry_size = 32 if is_fat64 else 20
    count = struct.unpack(">I", reader.read(4, 4, "universal Mach-O header"))[0]
    if count == 0 or count > _FAT_MAX_ARCHITECTURES:
        raise reader.fail(
            "universal Mach-O header declares {} architectures (not a "
            "universal binary).".format(count)
        )
    table = reader.read(8, count * entry_size, "universal Mach-O architecture table")
    architectures = []
    for index in range(count):
        base = index * entry_size
        cpu_type = struct.unpack_from(">i", table, base)[0]
        if is_fat64:
            slice_offset, slice_size = struct.unpack_from(">QQ", table, base + 8)
        else:
            slice_offset, slice_size = struct.unpack_from(">II", table, base + 8)
        label = "universal Mach-O slice {}".format(index)
        if slice_size == 0:
            raise reader.fail("{} is empty.".format(label))
        slice_cpu = _parse_macho_slice(reader, slice_offset, slice_size, label)
        if slice_cpu != cpu_type:
            raise reader.fail(
                "{} CPU type 0x{:x} disagrees with its table entry 0x{:x}.".format(
                    label, slice_cpu & 0xFFFFFFFF, cpu_type & 0xFFFFFFFF
                )
            )
        architectures.append(_machine_name(_MACHO_CPU_NAMES, cpu_type))
    return ExecutableInfo(
        format=FORMAT_MACHO,
        architectures=tuple(architectures),
        file_size=reader.file_size,
        universal=True,
    )


def _parse_elf(reader: _HeaderReader) -> ExecutableInfo:
    """Parse an ELF64 little-endian header, program headers, and interpreter."""

    header = reader.read(0, _ELF64_HEADER_SIZE, "ELF header")
    if header[4] != _ELFCLASS64:
        raise reader.fail("ELF file is not 64-bit (ELFCLASS64).")
    if header[5] != _ELFDATA2LSB:
        raise reader.fail("ELF file is not little-endian.")
    if header[6] != _EV_CURRENT:
        raise reader.fail(
            "ELF file has unknown identification version {}.".format(header[6])
        )
    (
        elf_type,
        machine,
        _,
        _,
        program_offset,
        section_offset,
        _,
        header_size,
        program_entry_size,
        program_count,
        section_entry_size,
        section_count,
        _,
    ) = struct.unpack_from("<HHIQQQIHHHHHH", header, 16)
    if elf_type not in (_ET_EXEC, _ET_DYN):
        raise reader.fail("ELF type {} is not an executable.".format(elf_type))
    if header_size < _ELF64_HEADER_SIZE:
        raise reader.fail("ELF header size {} is too small.".format(header_size))

    if program_count == _PN_XNUM or (section_offset and section_count == 0):
        # Extended numbering keeps the real counts in section header 0.
        if section_offset == 0 or section_entry_size < _ELF64_SHDR_SIZE:
            raise reader.fail("ELF extended header counts are unreadable.")
        section_zero = reader.read(
            section_offset, _ELF64_SHDR_SIZE, "ELF section header 0"
        )
        zero_size, _, zero_info = struct.unpack_from("<QII", section_zero, 32)
        if program_count == _PN_XNUM:
            program_count = zero_info
        if section_count == 0:
            section_count = zero_size

    if program_count == 0:
        raise reader.fail("ELF file has no program headers.")
    if program_entry_size < _ELF64_PHDR_SIZE:
        raise reader.fail("ELF program header entries are undersized.")
    table_bytes = program_entry_size * program_count
    if table_bytes > _ELF_MAX_PROGRAM_HEADER_BYTES:
        raise reader.fail("ELF program header table is implausibly large.")
    table = reader.read(program_offset, table_bytes, "ELF program header table")
    if section_offset or section_count:
        if section_entry_size < _ELF64_SHDR_SIZE:
            raise reader.fail("ELF section header entries are undersized.")
        reader.require_extent(
            section_offset,
            section_entry_size * section_count,
            "ELF section header table",
        )

    load_count = 0
    interpreter: Optional[str] = None
    for index in range(program_count):
        segment_type, _, segment_offset, _, _, segment_size = struct.unpack_from(
            "<IIQQQQ", table, index * program_entry_size
        )
        if segment_type == _PT_LOAD:
            load_count += 1
            reader.require_extent(
                segment_offset, segment_size, "ELF loadable segment {}".format(index)
            )
        elif segment_type == _PT_INTERP and interpreter is None:
            interpreter = _read_elf_interpreter(reader, segment_offset, segment_size)
    if load_count == 0:
        raise reader.fail("ELF file has no loadable segments.")

    return ExecutableInfo(
        format=FORMAT_ELF,
        architectures=(_machine_name(_ELF_MACHINE_NAMES, machine),),
        file_size=reader.file_size,
        interpreter=interpreter,
    )


def _read_elf_interpreter(reader: _HeaderReader, offset: int, size: int) -> str:
    """Return the NUL-terminated absolute path stored in ``PT_INTERP``."""

    if size < 2 or size > _ELF_MAX_INTERPRETER_BYTES:
        raise reader.fail(
            "ELF interpreter segment has implausible size {}.".format(size)
        )
    raw = reader.read(offset, size, "ELF interpreter path")
    # The Linux loader refuses an interpreter segment whose last byte is not
    # NUL, so a missing terminator means a corrupt or hostile header.
    if raw[-1:] != b"\0":
        raise reader.fail("ELF interpreter path is not NUL-terminated.")
    value = raw.split(b"\0", 1)[0]
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise reader.fail("ELF interpreter path is not valid UTF-8.") from exc
    if not text.startswith("/"):
        raise reader.fail("ELF interpreter path {!r} is not absolute.".format(text))
    return text


def _open_flags() -> int:
    """Return read-only open flags that refuse symlinks and FIFO blocking."""

    return (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def describe_executable(path: Path) -> ExecutableInfo:
    """Parse a native executable's headers without reading its body.

    Accepts only a regular file (never a symlink, directory, or device).
    Raises PayloadError for an unreadable, unrecognized, or structurally
    inconsistent file. Does not apply the minimum-size or platform checks;
    ``check_executable_structure`` adds those.
    """

    path = Path(path)
    descriptor = -1
    try:
        mode = os.lstat(path).st_mode
        if not stat.S_ISREG(mode):
            raise PayloadError("Executable {} is not a regular file.".format(path))
        descriptor = os.open(str(path), _open_flags())
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise PayloadError("Executable {} is not a regular file.".format(path))
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            reader = _HeaderReader(handle, path, file_stat.st_size)
            magic = reader.read(0, 4, "file magic")
            if magic == b"\x7fELF":
                return _parse_elf(reader)
            if magic[:2] == b"MZ":
                return _parse_pe(reader)
            if (
                magic in _MACHO64_MAGICS
                or magic in _MACHO32_MAGICS
                or magic in (_FAT_MAGIC, _FAT_MAGIC_64)
            ):
                return _parse_macho(reader, magic)
            raise reader.fail("is not a PE, Mach-O, or ELF executable.")
    except OSError as exc:
        raise PayloadError("Cannot read executable {}: {}".format(path, exc)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def elf_interpreter(path: Path) -> Optional[str]:
    """Return an ELF executable's ``PT_INTERP`` path, or None when static."""

    info = describe_executable(path)
    if info.format != FORMAT_ELF:
        raise PayloadError(
            "Executable {} is {}, not ELF; it has no program interpreter.".format(
                path, info.format
            )
        )
    return info.interpreter


def _check_libc_flavor(path: Path, platform_key: str, interpreter: str) -> None:
    """Reject an interpreter that names the other Linux C library.

    Only unambiguous loader names are judged (glibc's ``ld-linux-*`` and
    musl's ``ld-musl-*``), so an unfamiliar loader name never blocks an
    install on its own; the host-existence check still applies to it.
    """

    interpreter_name = interpreter.rsplit("/", 1)[-1]
    names_musl = interpreter_name.startswith("ld-musl")
    names_glibc = interpreter_name.startswith("ld-linux")
    if platform_key.endswith("-musl") and names_glibc:
        raise PayloadError(
            "Executable {} uses glibc interpreter {} but {} requires a musl "
            "build.".format(path, interpreter, platform_key)
        )
    if not platform_key.endswith("-musl") and names_musl:
        raise PayloadError(
            "Executable {} uses musl interpreter {} but {} requires a glibc "
            "build.".format(path, interpreter, platform_key)
        )


def check_executable_structure(
    path: Path,
    platform_key: str,
    *,
    min_size: int = MIN_EXECUTABLE_SIZE,
    check_interpreter: Optional[bool] = None,
    interpreter_exists: Optional[Callable[[str], bool]] = None,
) -> None:
    """Confirm a file is a plausible native binary for ``platform_key``.

    Checks the executable format (PE, Mach-O, or ELF), the CPU type (a
    universal Mach-O passes when it carries the expected slice), header
    consistency, and the minimum size. For ELF it also rejects a program
    interpreter naming the wrong C library and, when ``check_interpreter``
    is true, an interpreter that does not exist on this host.
    ``check_interpreter`` defaults to true only on Linux hosts, so a
    Windows or macOS host preparing a checkout for WSL or another machine
    still gets every other check. Raises PayloadError (or PlatformError for
    an unknown key) and never executes the file.
    """

    expected_format, expected_arch = _require_known_key(platform_key)
    path = Path(path)
    info = describe_executable(path)
    if info.file_size < min_size:
        raise PayloadError(
            "Executable {} is only {} bytes, below the {}-byte minimum for a "
            "native Claude Code binary.".format(path, info.file_size, min_size)
        )
    if info.format != expected_format:
        raise PayloadError(
            "Executable {} is in {} format, but {} requires {}.".format(
                path, info.format, platform_key, expected_format
            )
        )
    if expected_arch not in info.architectures:
        raise PayloadError(
            "Executable {} targets {}, but {} requires {}.".format(
                path, ", ".join(info.architectures), platform_key, expected_arch
            )
        )
    if info.format != FORMAT_ELF or info.interpreter is None:
        return

    _check_libc_flavor(path, platform_key, info.interpreter)
    should_check = (
        sys.platform.startswith("linux")
        if check_interpreter is None
        else check_interpreter
    )
    if not should_check:
        return
    exists = os.path.exists if interpreter_exists is None else interpreter_exists
    if not exists(info.interpreter):
        raise PayloadError(
            "Executable {} needs program interpreter {}, which does not exist "
            "on this host; {} does not match this system's C library or "
            "architecture.".format(path, info.interpreter, platform_key)
        )
