"""Static Python 3.9 and standard-library-only guards for shipped code.

`native_runtime_distribution_and_verification.md` requirement 14 keeps the
fetch and verification code Python 3.9-compatible and standard-library-only,
and the bootstrap entry points share the 3.9 floor. No Python 3.9
interpreter runs in every environment, so these checks parse each module
with the 3.9 grammar and walk its syntax tree for the 3.10+ APIs this code
base is most likely to reach for. They complement, not replace, a CI run on
Python 3.9.
"""

from __future__ import annotations

import ast
from pathlib import Path
import sys
from typing import Iterator, List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "src" / "claude_wrangler"
# Bootstrap scripts this repository owns (TheKnowledge-managed scripts are
# checked upstream).
OWNED_BOOTSTRAP_SCRIPTS = (
    REPO_ROOT / "scripts" / "install-stage-1-windows.py",
    REPO_ROOT / "scripts" / "install_prerequisites.py",
    REPO_ROOT / "scripts" / "install_project.py",
    REPO_ROOT / "scripts" / "install_user_tool.py",
)
# Modules on the fetch, verify, promote, and launch path: standard library
# plus this package's own relative imports only.
FETCH_AND_VERIFY_MODULES = (
    "atomicio",
    "distribution",
    "download",
    "launcher_core",
    "platforms",
    "releases",
    "services",
    "signing",
    "slots",
    "transaction",
)

# (call name, keyword) pairs that first appeared in Python 3.10 or later.
NEW_KEYWORDS = {
    ("zip", "strict"),
    ("realpath", "strict"),
    ("TemporaryDirectory", "ignore_cleanup_errors"),
    ("NamedTemporaryFile", "delete_on_close"),
    ("dataclass", "slots"),
    ("dataclass", "kw_only"),
    ("dataclass", "match_args"),
    ("field", "kw_only"),
    ("rmtree", "onexc"),
    ("relative_to", "walk_up"),
    ("Popen", "process_group"),
    ("run", "process_group"),
}
# Dotted attribute chains that first appeared in Python 3.10 or later.
NEW_ATTRIBUTES = {
    "hashlib.file_digest",
    "datetime.UTC",
    "itertools.pairwise",
    "itertools.batched",
    "contextlib.chdir",
    "contextlib.aclosing",
    "enum.StrEnum",
    "os.path.isjunction",
    "os.path.splitroot",
    "ntpath.splitroot",
    "posixpath.splitroot",
    "inspect.get_annotations",
}
NEW_METHODS = {"is_junction", "bit_count", "with_segments", "full_match"}
NEW_TYPING_NAMES = {
    "ParamSpec",
    "Concatenate",
    "TypeGuard",
    "TypeAlias",
    "Self",
    "LiteralString",
    "Never",
    "assert_never",
    "override",
    "Required",
    "NotRequired",
    "Unpack",
    "TypeVarTuple",
}
NEW_BUILTINS = {"ExceptionGroup", "BaseExceptionGroup", "aiter", "anext"}


def shipped_sources() -> List[Path]:
    """Return every package module plus the owned bootstrap scripts."""

    return sorted(PACKAGE_DIR.glob("*.py")) + list(OWNED_BOOTSTRAP_SCRIPTS)


def _label(path: Path) -> str:
    """Return a stable, repository-relative test id for a source file."""

    return path.relative_to(REPO_ROOT).as_posix()


def _dotted(node: ast.AST) -> str:
    """Return ``a.b.c`` for an attribute chain rooted at a name, else ``""``."""

    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _call_name(node: ast.Call) -> str:
    """Return the final name of a call's callee."""

    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _has_future_annotations(tree: ast.Module) -> bool:
    """Return whether a module postpones annotation evaluation."""

    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def _annotations(tree: ast.Module) -> Iterator[ast.AST]:
    """Yield every annotation expression in a module."""

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            every = (
                arguments.posonlyargs
                + arguments.args
                + arguments.kwonlyargs
                + [arguments.vararg, arguments.kwarg]
            )
            for argument in every:
                if argument is not None and argument.annotation is not None:
                    yield argument.annotation
            if node.returns is not None:
                yield node.returns
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation


def python310_findings(source: str) -> List[Tuple[int, str]]:
    """Return ``(line, description)`` for 3.10+ constructs found in source."""

    tree = ast.parse(source)
    findings: List[Tuple[int, str]] = []
    if not _has_future_annotations(tree):
        for annotation in _annotations(tree):
            for node in ast.walk(annotation):
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                    findings.append((node.lineno, "runtime X | Y annotation"))
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if type(node).__name__ in ("Match", "TryStar"):
            findings.append((line, "{} statement".format(type(node).__name__)))
        elif isinstance(node, ast.Attribute):
            dotted = _dotted(node)
            if dotted in NEW_ATTRIBUTES:
                findings.append((line, "3.10+ attribute {}".format(dotted)))
            elif node.attr in NEW_METHODS and isinstance(node.ctx, ast.Load):
                findings.append((line, "3.10+ method .{}".format(node.attr)))
            elif dotted.startswith("typing.") and node.attr in NEW_TYPING_NAMES:
                findings.append((line, "3.10+ {}".format(dotted)))
        elif isinstance(node, ast.ImportFrom) and node.module == "typing":
            for alias in node.names:
                if alias.name in NEW_TYPING_NAMES:
                    findings.append((line, "3.10+ typing.{}".format(alias.name)))
        elif isinstance(node, ast.Name) and node.id in NEW_BUILTINS:
            findings.append((line, "3.11+ builtin {}".format(node.id)))
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            for keyword in node.keywords:
                if (name, keyword.arg) in NEW_KEYWORDS:
                    findings.append(
                        (line, "{}({}=) is 3.10+".format(name, keyword.arg))
                    )
            if name in ("isinstance", "issubclass") and len(node.args) == 2:
                if isinstance(node.args[1], ast.BinOp):
                    findings.append((line, "{} with X | Y".format(name)))
            if name in ("from_bytes", "to_bytes") and len(node.args) < 2:
                if not any(keyword.arg == "byteorder" for keyword in node.keywords):
                    findings.append((line, "{} without byteorder".format(name)))
    return findings


@pytest.mark.parametrize("path", shipped_sources(), ids=_label)
def test_source_parses_with_the_python_39_grammar(path: Path) -> None:
    ast.parse(
        path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9)
    )


@pytest.mark.parametrize("path", shipped_sources(), ids=_label)
def test_source_uses_no_python_310_apis(path: Path) -> None:
    findings = python310_findings(path.read_text(encoding="utf-8"))
    assert findings == [], findings


@pytest.mark.parametrize("module", FETCH_AND_VERIFY_MODULES)
def test_fetch_and_verify_modules_import_only_the_standard_library(
    module: str,
) -> None:
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is None:
        pytest.skip("sys.stdlib_module_names needs Python 3.10+")
    path = PACKAGE_DIR / "{}.py".format(module)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    outside: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # This package's own relative imports; launcher_core has its
                # own stricter guard that forbids them.
                continue
            roots = [(node.module or "").split(".")[0]]
        else:
            continue
        outside.extend(
            root for root in roots if root not in stdlib and root != "__future__"
        )
    assert outside == [], "{} imports {}".format(module, outside)


def test_the_scanner_catches_known_python_310_constructs() -> None:
    sample = (
        "import hashlib\n"
        "from typing import TypeAlias\n"
        "def f(x: int | None) -> None:\n"
        "    for a, b in zip([1], [2], strict=True):\n"
        "        pass\n"
        "    hashlib.file_digest(x, 'sha256')\n"
        "    isinstance(x, int | str)\n"
        "    int.from_bytes(b'x')\n"
        "    match x:\n"
        "        case _:\n"
        "            pass\n"
    )
    found = {description for _line, description in python310_findings(sample)}
    assert found >= {
        "runtime X | Y annotation",
        "zip(strict=) is 3.10+",
        "3.10+ attribute hashlib.file_digest",
        "isinstance with X | Y",
        "from_bytes without byteorder",
        "Match statement",
        "3.10+ typing.TypeAlias",
    }
    with pytest.raises(SyntaxError):
        ast.parse(sample, feature_version=(3, 9))
