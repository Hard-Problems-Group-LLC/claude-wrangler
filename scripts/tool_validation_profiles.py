"""Helpers for `tool_validation_profiles.json` and Black profile resolution."""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Sequence

CONFIG_FILE_NAME = "tool_validation_profiles.json"

DEFAULT_TOOL_VALIDATION_PROFILES: Dict[str, object] = {
    "schema_version": "1.0.0",
    "runtime_policies": {
        "bootstrap_python": {
            "minimum_version": "3.9",
            "environment_variables": ["THEKNOWLEDGE_BOOTSTRAP_PYTHON"],
            "preferred_executables": [
                "python3.12",
                "python3.11",
                "python3.10",
                "python3.9",
                "python3",
                "python",
            ],
            "required_modules": [],
        },
        "steady_state_python_tools": {
            "minimum_version": "3.12",
            "environment_variables": [
                "THEKNOWLEDGE_BLACK_PYTHON",
                "THEKNOWLEDGE_PYTHON_TOOLS",
            ],
            "preferred_executables": [
                ".venv/bin/python",
                ".venv/Scripts/python.exe",
                "python3.12",
                "python",
                "python3",
            ],
            "required_modules": ["black"],
        },
    },
    "tools": {
        "black": {
            "file_suffixes": [".ipynb", ".py", ".pyi"],
            "default_roots": [
                "src",
                "tests",
                "scripts",
                "standards-and-practices/dev-utils",
                "templates/scripts",
            ],
            "defense_in_depth_excluded_dirs": [
                ".codex-home",
                ".codex-local",
                ".git",
                ".mypy_cache",
                ".pytest_cache",
                ".ruff_cache",
                ".venv",
                "__pycache__",
                "node_modules",
                "project.egg-info",
            ],
            "profiles": [
                {
                    "name": "starter_python",
                    "path_prefixes": ["templates/scripts/"],
                    "extensions": [".py", ".pyi"],
                    "target_version": "py39",
                    "runtime_policy": "steady_state_python_tools",
                    "allow_serial_fallback": True,
                },
                {
                    "name": "repository_python",
                    "path_prefixes": [
                        "scripts/",
                        "src/",
                        "tests/",
                        "standards-and-practices/dev-utils/",
                    ],
                    "extensions": [".py", ".pyi"],
                    "target_version": "py39",
                    "runtime_policy": "steady_state_python_tools",
                    "allow_serial_fallback": True,
                },
                {
                    "name": "python_notebooks",
                    "path_prefixes": [],
                    "extensions": [".ipynb"],
                    "target_version": "py310",
                    "runtime_policy": "steady_state_python_tools",
                    "allow_serial_fallback": True,
                    "default_profile": True,
                },
            ],
        }
    },
}


def _deep_copy_defaults() -> Dict[str, object]:
    return copy.deepcopy(DEFAULT_TOOL_VALIDATION_PROFILES)


def _config_path(repo_root: Path) -> Path:
    return repo_root / CONFIG_FILE_NAME


def load_tool_validation_profiles(repo_root: Path) -> Dict[str, object]:
    config_path = _config_path(repo_root)
    if not config_path.is_file():
        return _deep_copy_defaults()

    with config_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("tool validation profiles must be a JSON object")
    return data


def black_tool_config(repo_root: Path) -> Dict[str, object]:
    config = load_tool_validation_profiles(repo_root)
    tools = config.get("tools")
    if not isinstance(tools, dict):
        raise ValueError("tool validation profiles must define 'tools'")
    black = tools.get("black")
    if not isinstance(black, dict):
        raise ValueError("tool validation profiles must define tools.black")
    return black


def black_default_roots(repo_root: Path) -> List[str]:
    config = black_tool_config(repo_root)
    roots = config.get("default_roots")
    if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
        raise ValueError("tools.black.default_roots must be a list of strings")
    return list(roots)


def black_profiles(repo_root: Path) -> List[Dict[str, object]]:
    config = black_tool_config(repo_root)
    profiles = config.get("profiles")
    if not isinstance(profiles, list) or not all(
        isinstance(profile, dict) for profile in profiles
    ):
        raise ValueError("tools.black.profiles must be a list of objects")
    return list(profiles)


def black_file_suffixes(repo_root: Path) -> set[str]:
    config = black_tool_config(repo_root)
    suffixes = config.get("file_suffixes")
    if not isinstance(suffixes, list) or not all(
        isinstance(suffix, str) for suffix in suffixes
    ):
        raise ValueError("tools.black.file_suffixes must be a list of strings")
    return {suffix.lower() for suffix in suffixes}


def black_excluded_dirs(repo_root: Path) -> set[str]:
    config = black_tool_config(repo_root)
    excluded = config.get("defense_in_depth_excluded_dirs")
    if not isinstance(excluded, list) or not all(
        isinstance(name, str) for name in excluded
    ):
        raise ValueError(
            "tools.black.defense_in_depth_excluded_dirs must be a list of strings"
        )
    return set(excluded)


def _relative_repo_path(repo_root: Path, candidate: Path) -> str | None:
    try:
        return candidate.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None


def _path_is_black_candidate(
    repo_root: Path,
    relative_path: str,
    *,
    suffixes: set[str],
    excluded_dirs: set[str],
) -> bool:
    path = Path(relative_path)
    if path.suffix.lower() not in suffixes:
        return False
    return not any(part in excluded_dirs for part in path.parts)


def _git_paths(repo_root: Path, args: Sequence[str]) -> List[str] | None:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    stdout = result.stdout or ""
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def path_is_git_ignored(repo_root: Path, relative_path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", relative_path],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _walk_black_paths(
    repo_root: Path,
    roots: Sequence[str],
    *,
    suffixes: set[str],
    excluded_dirs: set[str],
    include_ignored: bool,
) -> List[str]:
    discovered: List[str] = []
    for root_name in roots:
        root_path = (repo_root / root_name).resolve()
        if not root_path.exists():
            continue
        if root_path.is_file():
            relative_path = _relative_repo_path(repo_root, root_path)
            if relative_path is None:
                continue
            if not _path_is_black_candidate(
                repo_root,
                relative_path,
                suffixes=suffixes,
                excluded_dirs=excluded_dirs,
            ):
                continue
            if not include_ignored and path_is_git_ignored(repo_root, relative_path):
                continue
            discovered.append(relative_path)
            continue

        for current_root, dirnames, filenames in os.walk(root_path):
            current_path = Path(current_root)
            dirnames[:] = [
                name
                for name in dirnames
                if name not in excluded_dirs
                and not (
                    not include_ignored
                    and path_is_git_ignored(
                        repo_root,
                        _relative_repo_path(repo_root, current_path / name) or "",
                    )
                )
            ]
            for filename in filenames:
                candidate = current_path / filename
                relative_path = _relative_repo_path(repo_root, candidate)
                if relative_path is None:
                    continue
                if not _path_is_black_candidate(
                    repo_root,
                    relative_path,
                    suffixes=suffixes,
                    excluded_dirs=excluded_dirs,
                ):
                    continue
                if not include_ignored and path_is_git_ignored(
                    repo_root, relative_path
                ):
                    continue
                discovered.append(relative_path)
    return sorted(set(discovered))


def _git_tracked_black_paths(
    repo_root: Path,
    roots: Sequence[str],
    *,
    suffixes: set[str],
    excluded_dirs: set[str],
) -> List[str] | None:
    tracked = _git_paths(repo_root, ["ls-files", "--", *roots])
    if tracked is None:
        return None
    filtered = [
        path
        for path in tracked
        if _path_is_black_candidate(
            repo_root,
            path,
            suffixes=suffixes,
            excluded_dirs=excluded_dirs,
        )
    ]
    return sorted(set(filtered))


def discover_black_paths(
    repo_root: Path,
    tokens: Sequence[str] | None = None,
    *,
    include_ignored: bool = False,
) -> List[str] | None:
    suffixes = black_file_suffixes(repo_root)
    excluded_dirs = black_excluded_dirs(repo_root)

    if not tokens:
        roots = black_default_roots(repo_root)
        tracked = _git_tracked_black_paths(
            repo_root,
            roots,
            suffixes=suffixes,
            excluded_dirs=excluded_dirs,
        )
        if tracked is not None:
            return tracked
        return _walk_black_paths(
            repo_root,
            roots,
            suffixes=suffixes,
            excluded_dirs=excluded_dirs,
            include_ignored=include_ignored,
        )

    discovered: List[str] = []
    for token in tokens:
        if token == "-":
            return None
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = (repo_root / candidate).resolve()
        if not candidate.exists():
            return None
        relative_path = _relative_repo_path(repo_root, candidate)
        if relative_path is None:
            return None
        if candidate.is_file():
            if not _path_is_black_candidate(
                repo_root,
                relative_path,
                suffixes=suffixes,
                excluded_dirs=excluded_dirs,
            ):
                return None
            if not include_ignored and path_is_git_ignored(repo_root, relative_path):
                continue
            discovered.append(relative_path)
            continue
        if not candidate.is_dir():
            return None
        tracked = _git_tracked_black_paths(
            repo_root,
            [relative_path],
            suffixes=suffixes,
            excluded_dirs=excluded_dirs,
        )
        if tracked is not None:
            discovered.extend(tracked)
            continue
        discovered.extend(
            _walk_black_paths(
                repo_root,
                [relative_path],
                suffixes=suffixes,
                excluded_dirs=excluded_dirs,
                include_ignored=include_ignored,
            )
        )

    return sorted(set(discovered))


def resolve_black_profile(repo_root: Path, relative_path: str) -> Dict[str, object]:
    path = relative_path
    default_profile: Dict[str, object] | None = None
    for profile in black_profiles(repo_root):
        extensions = profile.get("extensions")
        if not isinstance(extensions, list) or not all(
            isinstance(extension, str) for extension in extensions
        ):
            raise ValueError("Black profile extensions must be a list of strings")
        if Path(path).suffix.lower() not in {
            extension.lower() for extension in extensions
        }:
            continue
        if profile.get("default_profile") is True:
            default_profile = profile
            continue
        prefixes = profile.get("path_prefixes")
        if not isinstance(prefixes, list) or not all(
            isinstance(prefix, str) for prefix in prefixes
        ):
            raise ValueError("Black profile path_prefixes must be a list of strings")
        if any(path.startswith(prefix) for prefix in prefixes):
            return profile
    if default_profile is not None:
        return default_profile
    raise ValueError(f"no Black profile matched {relative_path}")


def group_black_paths_by_profile(
    repo_root: Path,
    relative_paths: Sequence[str],
) -> List[tuple[Dict[str, object], List[str]]]:
    grouped_paths: Dict[str, List[str]] = {}
    profile_map: Dict[str, Dict[str, object]] = {}
    for relative_path in sorted(set(relative_paths)):
        profile = resolve_black_profile(repo_root, relative_path)
        name = str(profile.get("name"))
        profile_map[name] = profile
        grouped_paths.setdefault(name, []).append(relative_path)

    groups: List[tuple[Dict[str, object], List[str]]] = []
    for profile in black_profiles(repo_root):
        name = str(profile.get("name"))
        paths = grouped_paths.get(name)
        if paths:
            groups.append((profile_map[name], paths))
    return groups


def _runtime_policies(repo_root: Path) -> Dict[str, object]:
    config = load_tool_validation_profiles(repo_root)
    policies = config.get("runtime_policies")
    if not isinstance(policies, dict):
        raise ValueError("tool validation profiles must define runtime_policies")
    return policies


def _parse_minimum_version(version_text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version_text.split("."))


def _normalize_executable_candidate(repo_root: Path, candidate: str) -> str:
    path_like = os.sep in candidate or "/" in candidate or "\\" in candidate
    if path_like:
        path = Path(candidate)
        if not path.is_absolute():
            path = (repo_root / path).resolve()
        return str(path)
    return candidate


def _probe_python_candidate(
    executable: str,
    required_modules: Sequence[str],
) -> tuple[tuple[int, ...], bool] | None:
    command = [
        executable,
        "-c",
        (
            "import importlib.util, json, sys; "
            f"modules = {json.dumps(list(required_modules))}; "
            "print(json.dumps({"
            "'version': list(sys.version_info[:3]), "
            "'modules': {name: importlib.util.find_spec(name) is not None "
            "for name in modules}"
            "}))"
        ),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return None
    version = payload.get("version")
    modules = payload.get("modules")
    if not isinstance(version, list) or not all(
        isinstance(part, int) for part in version
    ):
        return None
    if not isinstance(modules, dict):
        return None
    modules_ok = all(bool(modules.get(name)) for name in required_modules)
    return tuple(version), modules_ok


def resolve_runtime_policy_executable(
    repo_root: Path,
    policy_name: str,
    *,
    explicit_candidate: str | None = None,
    required_modules: Sequence[str] | None = None,
) -> str:
    policies = _runtime_policies(repo_root)
    policy = policies.get(policy_name)
    if not isinstance(policy, dict):
        raise ValueError(f"unknown runtime policy '{policy_name}'")

    minimum_version = policy.get("minimum_version")
    if not isinstance(minimum_version, str):
        raise ValueError("runtime policy minimum_version must be a string")
    minimum = _parse_minimum_version(minimum_version)

    configured_modules = policy.get("required_modules", [])
    if not isinstance(configured_modules, list) or not all(
        isinstance(name, str) for name in configured_modules
    ):
        raise ValueError("runtime policy required_modules must be a list of strings")
    if required_modules is None:
        requested_modules = list(configured_modules)
    else:
        requested_modules = list(required_modules)

    if explicit_candidate is not None:
        candidate = _normalize_executable_candidate(repo_root, explicit_candidate)
        probe = _probe_python_candidate(candidate, requested_modules)
        if probe is None:
            raise RuntimeError(
                f"Python runtime '{explicit_candidate}' is not runnable."
            )
        version, modules_ok = probe
        if version < minimum:
            raise RuntimeError(
                f"Python runtime '{explicit_candidate}' is below the required "
                f"minimum version {minimum_version}."
            )
        if not modules_ok:
            raise RuntimeError(
                f"Python runtime '{explicit_candidate}' does not provide the "
                f"required modules: {', '.join(requested_modules)}."
            )
        return candidate

    candidates: List[str] = []
    env_vars = policy.get("environment_variables", [])
    if not isinstance(env_vars, list) or not all(
        isinstance(name, str) for name in env_vars
    ):
        raise ValueError("runtime policy environment_variables must be a list")
    for name in env_vars:
        value = os.environ.get(name)
        if value:
            candidates.append(value)

    preferred = policy.get("preferred_executables", [])
    if not isinstance(preferred, list) or not all(
        isinstance(name, str) for name in preferred
    ):
        raise ValueError("runtime policy preferred_executables must be a list")
    candidates.extend(preferred)

    seen: set[str] = set()
    for candidate in candidates:
        normalized = _normalize_executable_candidate(repo_root, candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        probe = _probe_python_candidate(normalized, requested_modules)
        if probe is None:
            continue
        version, modules_ok = probe
        if version < minimum or not modules_ok:
            continue
        return normalized

    modules_text = ", ".join(requested_modules) if requested_modules else "none"
    raise RuntimeError(
        f"No Python runtime matched policy '{policy_name}' with minimum version "
        f"{minimum_version} and required modules {modules_text}."
    )
