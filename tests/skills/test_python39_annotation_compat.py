"""Guard skill scripts against import-time crashes on Python 3.9 (#16257).

Skill scripts are documented to run standalone under a system interpreter
(e.g. macOS' ``/usr/bin/python3``, which is 3.9).  A PEP 604 union
(``str | None``) or PEP 585 builtin generic (``list[str]``) written in an
annotation is *evaluated* when the enclosing ``def`` executes, so on 3.9 the
module raises::

    TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'

before ``main()`` ever runs.  ``from __future__ import annotations`` makes
annotations lazy strings and removes that failure mode with no behaviour change.

``ast.parse`` cannot catch this - it only proves the file parses - so this
module asserts the structural invariant on every interpreter and, when a real
3.9 is available, additionally executes the scripts under it.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO_ROOT / "skills"

# The script named in #16257, plus the sibling hit found while verifying the
# sweep (identical failure mode, same root cause, reported on the issue thread).
ANNOTATION_TARGETS = [
    REPO_ROOT / "skills" / "productivity" / "google-workspace" / "scripts" / "google_api.py",
    REPO_ROOT / "skills" / "software-development" / "github" / "scripts" / "git-credential-token.py",
    REPO_ROOT / "skills" / "media" / "youtube-content" / "scripts" / "fetch_transcript.py",
]

# Everything that must survive a real pre-3.10 import. This is a superset of
# ANNOTATION_TARGETS: gws_bridge.py needs no future import, but it imports
# `datetime.UTC`, which only exists from Python 3.11 and so fails on 3.9 for an
# entirely different reason. Annotations alone cannot catch that -- only
# executing the module under a real interpreter can.
PY39_IMPORT_TARGETS = ANNOTATION_TARGETS + [
    REPO_ROOT / "skills" / "productivity" / "google-workspace" / "scripts" / "gws_bridge.py",
]

_BUILTIN_GENERICS = {"list", "dict", "set", "tuple", "frozenset", "type"}
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "build", "dist"}


def _defers_annotations(tree: ast.Module) -> bool:
    """True if the module opted out of eager annotation evaluation."""
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if any(alias.name == "annotations" for alias in node.names):
                return True
    return False


def _argument_annotations(args: ast.arguments) -> list[ast.AST]:
    out: list[ast.AST] = [a.annotation for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    if args.vararg:
        out.append(args.vararg.annotation)
    if args.kwarg:
        out.append(args.kwarg.annotation)
    return [a for a in out if a is not None]


def _unsafe_sites(tree: ast.Module) -> list[tuple[int, str]]:
    """(lineno, snippet) for annotations that 3.9 cannot evaluate eagerly.

    Only meaningful for modules *without* ``from __future__ import annotations``:
    PEP 604 (``X | Y``) and PEP 585 (``list[str]``) both raise on 3.9 when
    evaluated at definition time.
    """
    sites: list[tuple[int, str]] = []

    def record(node: ast.AST) -> None:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            sites.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            if node.value.id in _BUILTIN_GENERICS:
                sites.append((node.lineno, ast.unparse(node)))

    def scan(ann: ast.AST | None) -> None:
        if ann is None:
            return
        for sub in ast.walk(ann):
            # A quoted annotation ('str | None') is a plain string constant and
            # is never evaluated, so it is safe on its own.
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                continue
            record(sub)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for ann in _argument_annotations(node.args):
                scan(ann)
            scan(node.returns)
        elif isinstance(node, ast.AnnAssign):
            scan(node.annotation)
    return sites


def _iter_skill_scripts() -> list[Path]:
    return [
        path
        for path in sorted(SKILLS_ROOT.rglob("*.py"))
        if not (_SKIP_DIRS & set(path.parts))
    ]


@pytest.mark.parametrize("script", ANNOTATION_TARGETS, ids=lambda p: p.name)
def test_reported_scripts_defer_annotations(script: Path) -> None:
    """#16257: the reported scripts must opt out of eager annotation evaluation."""
    assert script.is_file(), f"expected script to exist: {script}"
    tree = ast.parse(script.read_text(encoding="utf-8"))
    assert _defers_annotations(tree), (
        f"{script.name} is missing `from __future__ import annotations`; its PEP 604 "
        f"annotations raise TypeError at import time on Python 3.9: "
        f"{_unsafe_sites(tree)[:3]}"
    )


def test_every_skill_script_defers_or_avoids_modern_annotations() -> None:
    """Sweep guard so the next script added cannot reintroduce the crash."""
    offenders: list[str] = []
    for script in _iter_skill_scripts():
        try:
            tree = ast.parse(script.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        if _defers_annotations(tree):
            continue
        unsafe = _unsafe_sites(tree)
        if unsafe:
            first_lineno, snippet = unsafe[0]
            rel = script.relative_to(REPO_ROOT)
            offenders.append(f"{rel}:{first_lineno} {snippet} (+{len(unsafe) - 1} more)")
    assert not offenders, (
        "skill scripts must either import `from __future__ import annotations` "
        "or avoid PEP 604/585 annotations:\n  " + "\n  ".join(offenders)
    )


_PROBE_SOURCE = '''
import importlib.abc, importlib.machinery, importlib.util, os, sys, types
from pathlib import Path

REPO_MODULES = set()


class _Stub(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        child = _Stub(f"{self.__name__}.{name}")
        setattr(self, name, child)
        return child


class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Stub only genuinely-absent third-party imports.

    Repo-root modules and sibling script modules must keep their real
    behaviour (or raise ModuleNotFoundError) so a script's own
    `try: ... except ImportError:` fallback still runs -- otherwise this probe
    would report failures the real interpreter never sees.
    """

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in sys.builtin_module_names:
            return None
        search_path = sys.path if path is None else path
        try:
            real = importlib.machinery.PathFinder.find_spec(fullname, search_path, target)
        except (ImportError, AttributeError, TypeError, ValueError):
            real = None
        if real is not None:
            return None
        if isinstance(sys.modules.get(fullname), _Stub):
            return None
        if root in REPO_MODULES:
            return None
        return importlib.machinery.ModuleSpec(fullname, self)

    def create_module(self, spec):
        return _Stub(spec.name)

    def exec_module(self, module):
        pass


for arg in sys.argv[1:]:
    path = Path(arg).resolve()
    probe_root = path
    while probe_root != probe_root.parent:
        if (probe_root / "hermes_constants.py").is_file():
            break
        probe_root = probe_root.parent
    else:
        probe_root = None
    if probe_root is not None:
        for entry in probe_root.glob("*.py"):
            if not entry.name.startswith("_"):
                REPO_MODULES.add(entry.stem)

    parent = str(path.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    importlib.invalidate_caches()

    name = "_probe_" + "".join(c if c.isalnum() else "_" for c in path.stem)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.stderr.write(f"{path.name}: {type(exc).__name__}: {exc}\\n")
        sys.exit(1)
sys.exit(0)
'''


def _python39() -> str | None:
    for candidate in ("python3.9", "python3.10"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


@pytest.mark.parametrize("script", PY39_IMPORT_TARGETS, ids=lambda p: p.name)
def test_reported_scripts_import_under_a_pre_310_interpreter(script: Path) -> None:
    """Execute the module for real -- parsing alone does not catch this."""
    interpreter = _python39()
    if interpreter is None:
        version = subprocess.run(
            [sys.executable, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if tuple(int(p) for p in version.split(".")[:2]) >= (3, 10):
            pytest.skip("no Python <3.10 interpreter available to execute the import probe")
        interpreter = sys.executable

    # Model the reported repro faithfully: `python3 <script>` puts the SCRIPT's
    # own directory on sys.path, not the repo root. Running with cwd=REPO_ROOT
    # would leak the repo root onto sys.path[0] and pull in hermes_constants,
    # which is itself not 3.9-safe and is not importable in a real install.
    result = subprocess.run(
        [interpreter, "-c", _PROBE_SOURCE, str(script)],
        capture_output=True,
        text=True,
        cwd=str(script.parent),
    )
    assert result.returncode == 0, (
        f"{script.name} failed to import under {interpreter}:\n{result.stderr.strip()}"
    )