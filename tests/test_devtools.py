import subprocess
from pathlib import Path

from claude_wrangler.devtools import (
    black_supports_flag,
    build_black_command,
    expand_black_targets,
    list_tracked_python_files,
    main_run_quality_gate,
    run_repo_quality_gate,
)


def test_build_black_command_uses_supported_no_cache_flag():
    command = build_black_command(
        "python3",
        Path("example.py"),
        supports_no_cache=True,
        check=True,
        diff=False,
    )
    assert "--no-cache" in command
    assert "--check" in command
    assert str(Path("example.py")) == command[-1]


def test_build_black_command_omits_unsupported_no_cache_flag():
    command = build_black_command(
        "python3",
        Path("example.py"),
        supports_no_cache=False,
        check=False,
        diff=True,
    )
    assert "--no-cache" not in command
    assert "--diff" in command


def test_expand_black_targets_defaults_to_tracked_python_files(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "tests" / "test_app.py").write_text(
        "def test_ok():\n    pass\n", encoding="utf-8"
    )

    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "add", "src/app.py", "tests/test_app.py"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    targets = expand_black_targets(tmp_path, ())

    assert tmp_path / "src" / "app.py" in targets
    assert tmp_path / "tests" / "test_app.py" in targets


def test_list_tracked_python_files_falls_back_when_git_scan_times_out(
    monkeypatch,
    tmp_path,
):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")

    def fake_run_capture(command, cwd, timeout_seconds=None):
        raise subprocess.TimeoutExpired(cmd=command, timeout=timeout_seconds or 0)

    monkeypatch.setattr("claude_wrangler.devtools._run_capture", fake_run_capture)

    targets = list_tracked_python_files(tmp_path)

    assert targets == [tmp_path / "src" / "app.py"]


def test_black_supports_flag_returns_false_when_help_probe_times_out(
    monkeypatch,
    tmp_path,
):
    def fake_run_capture(command, cwd, timeout_seconds=None):
        raise subprocess.TimeoutExpired(cmd=command, timeout=timeout_seconds or 0)

    monkeypatch.setattr("claude_wrangler.devtools._run_capture", fake_run_capture)

    assert black_supports_flag("python3", tmp_path, "--no-cache") is False


def test_black_supports_flag_returns_false_when_help_probe_fails(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        "claude_wrangler.devtools._run_capture",
        lambda command, cwd, timeout_seconds=None: subprocess.CompletedProcess(
            args=list(command),
            returncode=1,
            stdout="",
            stderr="boom",
        ),
    )

    assert black_supports_flag("python3", tmp_path, "--no-cache") is False


def test_run_repo_quality_gate_uses_repo_entropy_paths(monkeypatch, tmp_path):
    commands = []

    monkeypatch.setattr(
        "claude_wrangler.devtools.expand_black_targets",
        lambda repo_root, raw_targets: [tmp_path / "example.py"],
    )
    monkeypatch.setattr(
        "claude_wrangler.devtools.run_black_serial",
        lambda *args, **kwargs: 0,
    )

    def fake_run(command, cwd, timeout_seconds, label):
        commands.append((label, list(command)))
        return 0

    monkeypatch.setattr("claude_wrangler.devtools.run_timed_command", fake_run)

    exit_code = run_repo_quality_gate(
        "python3",
        tmp_path,
        ["ruff", "compileall", "entropy_check", "entropy_tripwire_verify", "pytest"],
        black_timeout_seconds=30,
    )

    assert exit_code == 0
    assert any(
        "TheKnowledge/standards-and-practices/dev-utils/security/run_entropy_harness.py"
        in command
        for _, command in commands
    )
    assert any(
        "TheKnowledge/standards-and-practices/dev-utils/security/verify_entropy_tripwire.py"
        in command
        for _, command in commands
    )


def test_main_run_quality_gate_defaults_to_full_tool_list(monkeypatch, tmp_path):
    recorded = {}

    def fake_run_repo_quality_gate(
        python_executable,
        repo_root,
        tools,
        black_timeout_seconds,
    ):
        recorded["tools"] = list(tools)
        recorded["timeout"] = black_timeout_seconds
        return 0

    monkeypatch.setattr(
        "claude_wrangler.devtools.run_repo_quality_gate",
        fake_run_repo_quality_gate,
    )

    exit_code = main_run_quality_gate(tmp_path, [])

    assert exit_code == 0
    assert recorded["tools"] == [
        "black",
        "ruff",
        "compileall",
        "entropy_check",
        "entropy_tripwire_verify",
        "pytest",
    ]
