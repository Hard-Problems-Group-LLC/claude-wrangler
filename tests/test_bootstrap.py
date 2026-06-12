from pathlib import Path
import importlib.util

from scripts.python_environment_bootstrap import (
    context_requires_virtualenv,
    DIRENV_BEGIN,
    DIRENV_END,
    ENVRC_MARKER,
    build_direnv_hook_block,
    build_envrc_content,
    detect_shell_name,
    direnv_download_name,
    load_python_environment_config,
    selection_name,
    shell_rc_path,
    upsert_managed_block,
)

ROOT = Path(__file__).resolve().parents[1]


def load_install_stage_2_module():
    module_path = ROOT / "scripts" / "install-stage-2.py"
    spec = importlib.util.spec_from_file_location("install_stage_2", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_load_python_environment_config_reads_runtime_selection():
    config = load_python_environment_config(ROOT)

    assert config.bootstrap.required_version == "3.9"
    assert config.runtime.environment_name == "3.12.12"
    assert not context_requires_virtualenv(config.runtime)
    assert selection_name(config.runtime) == "3.12.12"


def test_detect_shell_name_defaults_to_bash_when_empty():
    assert detect_shell_name("") == "bash"


def test_shell_rc_path_supports_bash_and_zsh(tmp_path):
    assert shell_rc_path(tmp_path, "bash") == tmp_path / ".bashrc"
    assert shell_rc_path(tmp_path, "zsh") == tmp_path / ".zshrc"
    assert shell_rc_path(tmp_path, "fish") is None


def test_build_direnv_hook_block_contains_managed_markers():
    block = build_direnv_hook_block("bash")

    assert DIRENV_BEGIN in block
    assert '*":$HOME/.local/bin:"*) ;;' in block
    assert 'eval "$(direnv hook bash)"' in block
    assert DIRENV_END in block


def test_upsert_managed_block_replaces_existing_block():
    original = "\n".join([DIRENV_BEGIN, "old", DIRENV_END, "tail", ""])
    replacement = "\n".join([DIRENV_BEGIN, "new", DIRENV_END, ""])

    updated = upsert_managed_block(original, DIRENV_BEGIN, DIRENV_END, replacement)

    assert "old" not in updated
    assert "new" in updated
    assert updated.endswith("tail\n")


def test_build_envrc_content_sources_repo_venv():
    content = build_envrc_content()

    assert ENVRC_MARKER in content
    assert 'source "$PWD/.venv/bin/activate"' in content


def test_direnv_download_name_supports_linux_targets():
    assert direnv_download_name("linux", "x86_64") == "direnv.linux-amd64"
    assert direnv_download_name("linux", "aarch64") == "direnv.linux-arm64"


# The install-git-hooks and ensure-direnv behaviors moved under direct
# upstream test coverage in TheKnowledge/tests/ (the newer starter
# changed those internals), so this repository does not duplicate those
# tests against private stage-2 attributes.
