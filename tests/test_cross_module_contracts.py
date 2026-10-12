"""Cross-module contracts that the standalone launcher core duplicates.

`launcher_core.py` is copied into each project and must import nothing
from the package, so it repeats constants and the host platform detector.
These tests keep both sides identical and check the file formats that cross
the boundary (launch.json, completion records, update-check.json).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from claude_wrangler import (
    constants,
    distribution,
    launcher_core,
    platforms,
    signing,
    slots,
)
from claude_wrangler.platforms import HostProbes
from claude_wrangler.rendering import build_launch_config, launch_config_text

from native_fakes import make_config


def test_slot_constants_match():
    assert launcher_core.SLOT_NAMES == slots.SLOT_NAMES
    assert launcher_core.POINTER_NAME == slots.POINTER_NAME
    assert launcher_core.COMPLETION_RECORD_NAME == slots.COMPLETION_RECORD_NAME
    assert launcher_core.SLOTS_DIR_NAME == slots.SLOTS_DIR_NAME
    assert launcher_core.VERIFICATION_STAMP_NAME == slots.VERIFICATION_STAMP_NAME
    assert launcher_core.VERIFICATION_STAMP_SCHEMA == slots.VERIFICATION_STAMP_SCHEMA


def test_binary_identity_matches(tmp_path):
    # The launcher compares exactly the identity maintenance records.
    path = tmp_path / "binary"
    path.write_bytes(b"x" * 5)
    status = path.stat()

    assert launcher_core._binary_identity(status) == slots.binary_identity(status)


def test_distribution_constants_match():
    assert (
        launcher_core.DEFAULT_RELEASE_BASE_URL == distribution.DEFAULT_RELEASE_BASE_URL
    )
    assert launcher_core.SUPPORTED_CHANNELS == distribution.SUPPORTED_CHANNELS
    assert launcher_core.UPDATE_CHECK_ALLOWED_HOST == distribution.DEFAULT_RELEASE_HOST
    assert constants.CLAUDE_CHANNELS == distribution.SUPPORTED_CHANNELS
    assert slots.EVIDENCE_FILE_NAMES == distribution.EVIDENCE_NAMES
    assert slots.MANIFEST_NAME == distribution.MANIFEST_NAME
    assert distribution.SIGNATURE_MAX_BYTES == signing.MAX_SIGNATURE_BYTES


def test_generated_file_names_and_settings_match():
    assert launcher_core.SHIM_DIR_NAME == constants.PATH_SHIM_DIRNAME
    assert launcher_core.FALLBACK_RECORD_NAME == constants.FALLBACK_FILENAME
    assert launcher_core.UPDATE_CHECK_NAME == constants.UPDATE_CHECK_FILENAME
    assert launcher_core.LAUNCH_CONFIG_NAME == constants.LAUNCH_CONFIG_FILENAME
    assert launcher_core.LAUNCH_CONFIG_SCHEMA == constants.LAUNCH_CONFIG_SCHEMA
    assert (
        launcher_core.REQUIRED_SUPPRESSION_ENV == constants.SELF_UPDATE_SUPPRESSION_ENV
    )
    assert launcher_core.CONFIG_DIR_SPELLINGS == constants.CONFIG_DIR_SPELLINGS
    assert launcher_core.MANAGED_BY_VALUE == constants.SCRIPT_NAME


def test_launcher_core_imports_only_the_standard_library():
    tree = ast.parse(Path(launcher_core.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import in launcher_core"
            assert not (node.module or "").startswith("claude_wrangler")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("claude_wrangler")


# (system, machine, rosetta, windows native machine, musl) -> expected key
FACTS = [
    (("Darwin", "arm64", False, None, False), "darwin-arm64"),
    (("Darwin", "x86_64", True, None, False), "darwin-arm64"),
    (("Darwin", "x86_64", False, None, False), "darwin-x64"),
    (("Linux", "x86_64", False, None, False), "linux-x64"),
    (("Linux", "aarch64", False, None, False), "linux-arm64"),
    (("Linux", "x86_64", False, None, True), "linux-x64-musl"),
    (("Linux", "arm64", False, None, True), "linux-arm64-musl"),
    (("Windows", "AMD64", False, "AMD64", False), "win32-x64"),
    (("Windows", "AMD64", False, "ARM64", False), "win32-arm64"),
    (("Windows", "ARM64", False, None, False), "win32-arm64"),
    (("Windows", "AMD64", False, None, False), "win32-x64"),
]


@pytest.mark.parametrize("facts, expected", FACTS)
def test_platform_detection_agrees_for_every_key(facts, expected):
    system, machine, rosetta, native, musl = facts
    package = platforms.host_platform_key(
        HostProbes(
            system=system,
            machine=machine,
            rosetta_translated=rosetta,
            windows_native_machine=native,
            musl=musl,
        )
    )
    core = launcher_core.platform_key_from_facts(
        system,
        machine,
        rosetta_translated=rosetta,
        windows_native_machine=native,
        musl=musl,
    )
    assert package == core == expected


def test_every_platform_key_is_covered():
    assert {expected for _facts, expected in FACTS} == set(platforms.PLATFORM_KEYS)


@pytest.mark.parametrize(
    "facts",
    [
        ("Windows", "x86", False, "x86", False),
        ("Linux", "ppc64le", False, None, False),
        ("Darwin", "powerpc", False, None, False),
        ("FreeBSD", "amd64", False, None, False),
    ],
)
def test_platform_detection_refuses_the_same_unsupported_hosts(facts):
    system, machine, rosetta, native, musl = facts
    with pytest.raises(platforms.PlatformError):
        platforms.host_platform_key(HostProbes(system, machine, rosetta, native, musl))
    with pytest.raises(launcher_core.UnsupportedHostError):
        launcher_core.platform_key_from_facts(
            system,
            machine,
            rosetta_translated=rosetta,
            windows_native_machine=native,
            musl=musl,
        )


def test_live_host_detection_agrees():
    assert launcher_core.detect_host_platform_key() == platforms.host_platform_key()


def test_rendered_launch_json_parses_in_the_core(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = make_config(project, "--set-reasonable-permissions", "--shared-home")
    data = json.loads(launch_config_text(build_launch_config(config)))
    parsed = launcher_core.parse_launch_config(data)
    assert parsed.reasonable_permissions is True
    assert parsed.shared_home_default is True
    assert parsed.wrangler_command == "claude-wrangler"
    legacy = launcher_core.parse_launch_config(
        build_launch_config(
            config, legacy_binary="a/b/claude", legacy_version="2.1.287"
        )
    )
    assert legacy.legacy_binary == ("a", "b", "claude")


def test_completion_records_round_trip_into_the_core(tmp_path):
    store = slots.RuntimeStore(tmp_path / "rt")
    slot_dir = store.slot_dir("a")
    binary = slot_dir / "linux-x64" / "claude"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"x" * 10)
    binary.chmod(0o755)
    record = slots.CompletionRecord(
        slot="a",
        version="2.1.296",
        channel="stable",
        platforms={
            "linux-x64": {"binary": "linux-x64/claude", "sha256": "0" * 64, "size": 10}
        },
        manifest_sha256="1" * 64,
        build_date=None,
        commit=None,
        schemes=["openpgp", "raw-rsa"],
        subcommands=["mcp"],
        verified_at="2026-10-12T00:00:00Z",
        promoted_by="test",
    )
    (slot_dir / slots.COMPLETION_RECORD_NAME).write_text(record.to_json(), "utf-8")
    check = launcher_core.check_slot(str(store.root), "a", "linux-x64")
    assert not check.ok and "verification stamp" in check.reason
    # The stamp slots.py writes is the one the launcher core reads.
    store.write_stamp("a", store.binary_identities("a", ["linux-x64"]))
    check = launcher_core.check_slot(str(store.root), "a", "linux-x64")
    assert check.ok, check.reason
    assert launcher_core.record_subcommands(check.record) == ("mcp",)
    assert launcher_core.record_settings_rejected(check.record) is None
    probed = slots.CompletionRecord.from_json(record.to_json())
    probed.settings_rejected = ["doctor"]
    (slot_dir / slots.COMPLETION_RECORD_NAME).write_text(probed.to_json(), "utf-8")
    check = launcher_core.check_slot(str(store.root), "a", "linux-x64")
    assert check.ok, check.reason
    assert launcher_core.record_settings_rejected(check.record) == ("doctor",)


def test_settings_probe_skips_exactly_the_intercepted_subcommands():
    from claude_wrangler import services

    assert services.NEVER_PROBED_SUBCOMMANDS == launcher_core.INTERCEPTED_SUBCOMMANDS
