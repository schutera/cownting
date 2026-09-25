"""The install itself is a deployment surface — and nothing else tests it.

Three invariants, each of which has exactly one way to break and no way to
notice:

1. `pyproject.toml` names its packages EXPLICITLY, which switches setuptools'
   auto-discovery off. So a new subpackage is shipped only if someone remembers
   to add it to the list. `cownting.instance_attrs` — the entire crop-classifier
   half of the app — was not on it. The documented Windows setup is
   `pip install -e .`, and an editable install resolves subpackages through a
   path hook regardless of the list, so the gap was invisible there; the
   container COPYs the source tree, so it was invisible there too. It would have
   surfaced only on a real `pip install .`, as `ModuleNotFoundError` at boot.

2. The optional and multi-gigabyte dependencies — torch, transformers, sam2,
   fiftyone — are imported INSIDE the functions that need them, never at module
   level. That discipline is what lets `cownting serve` boot without waiting on
   torch and lets `[label]`/`[finetune]` stay genuinely optional. One
   module-level `import torch` added out of habit silently ends both, and
   nothing fails: the dev box has torch installed.

3. Every third-party module the package imports is a dependency someone declared
   somewhere. An import satisfied only by a transitive pull is a dependency that
   vanishes when an unrelated package changes its own requirements.

No pytest. Run either way:
    py -3.11 -m tests.test_packaging
    py -3.11 tests/test_packaging.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import tomllib

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "cownting"

_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    line = f"[{'ok ' if cond else 'FAIL'}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1
        if os.environ.get("PYTEST_CURRENT_TEST"):
            raise AssertionError(line)


def _pyproject() -> dict:
    with open(ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(p: pathlib.Path) -> str:
    return p.relative_to(ROOT).as_posix()


# --------------------------------------------------------------------------- 1

def test_every_subpackage_on_disk_is_declared_in_pyproject():
    """THE BUG THIS PINS. An explicit `[tool.setuptools] packages` list is
    exhaustive — setuptools ships what it names and nothing else. Add a
    subpackage, forget the list, and `pip install .` produces an installation
    that imports fine on the machine that built it (the source tree is still on
    sys.path) and raises ModuleNotFoundError anywhere else."""
    declared = set(_pyproject()["tool"]["setuptools"]["packages"])
    on_disk = {"cownting"} | {
        "cownting." + d.name
        for d in sorted(PKG.iterdir())
        if d.is_dir() and (d / "__init__.py").exists()
    }
    check("every package directory on disk is declared",
          on_disk <= declared, f"undeclared: {sorted(on_disk - declared)}")
    check("every declared package exists on disk",
          declared <= on_disk, f"declared but absent: {sorted(declared - on_disk)}")


def test_the_console_script_entry_point_resolves():
    """`[project.scripts] cownting = "cownting.cli:app"` is what `pip install`
    turns into the `cownting` command. A rename of the Typer app object leaves
    the entry point pointing at nothing, and the failure appears only when
    someone runs the installed command."""
    ep = _pyproject()["project"]["scripts"]["cownting"]
    mod_name, _, attr = ep.partition(":")
    path = ROOT / (mod_name.replace(".", "/") + ".py")
    check("the entry-point module exists", path.exists(), ep)
    if not path.exists():
        return
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    check(f"`{attr}` is defined at module level in {mod_name}", attr in names, ep)


# --------------------------------------------------------------------------- 2

# Imported lazily, on purpose, and the reason differs per package:
#   torch/torchvision  multi-GB, installed from a separate wheel index by both
#                      the Dockerfile and CI — never a plain `pip install`
#   ultralytics        drags torch in behind it
#   transformers       only the optional pose / grounded-sam2 backends
#   sam2               not on PyPI at all; installed from git by hand
#   fiftyone           only the `[label]` annotation round-trip
LAZY_ONLY = {"torch", "torchvision", "ultralytics", "transformers", "sam2", "fiftyone"}


def _module_level_imports(tree: ast.Module):
    """Top-level statements only — plus the bodies of `if`/`try` at top level,
    which execute on import just the same. A `try: import torch` guarded by
    ImportError still pays torch's import cost on every boot that has it."""
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], node.lineno
        elif isinstance(node, ast.ImportFrom):
            if not node.level and node.module:
                yield node.module.split(".")[0], node.lineno
        elif isinstance(node, (ast.If, ast.Try)):
            stack.extend(node.body)
            stack.extend(node.orelse)
            stack.extend(getattr(node, "finalbody", []))
            for handler in getattr(node, "handlers", []):
                stack.extend(handler.body)


def test_the_heavy_dependencies_are_imported_lazily():
    """`cownting serve` must boot without importing torch, and `[label]` /
    `[finetune]` must stay genuinely optional. Both properties are held up by
    nothing but the habit of putting these imports inside the function that
    needs them."""
    offenders = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for mod, lineno in _module_level_imports(tree):
            if mod in LAZY_ONLY:
                offenders.append(f"{_rel(path)}:{lineno} imports {mod}")
    check("no module-level import of a heavy or optional dependency",
          not offenders, "; ".join(sorted(offenders)))


def test_the_optional_backends_are_not_reachable_from_a_plain_boot():
    """The modules that own the optional backends may import them lazily, but
    they must also not be dragged in by the serving path. If `pipeline.py`
    imported `grounded_sam2` at module level, the `transformers` dependency
    would be back — via a module that never runs."""
    optional_modules = {"cownting.detect.grounded_sam2", "cownting.detect.pose",
                        "cownting.finetune.dataset", "cownting.finetune.export_cvat"}
    serving = [PKG / "api.py", PKG / "cli.py", PKG / "pipeline.py", PKG / "uploads.py"]
    offenders = []
    for path in serving:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in tree.body:
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                # `from .detect import pose` and `from cownting.detect import pose`
                # name the same module; normalise both to the dotted path.
                stem = node.module or ""
                if node.level:                       # relative, i.e. inside cownting
                    stem = f"cownting.{stem}".rstrip(".")
                targets = [stem] + [f"{stem}.{a.name}".strip(".") for a in node.names]
            for t in targets:
                if t in optional_modules:
                    offenders.append(f"{_rel(path)}:{node.lineno} -> {t}")
    check("the serving modules do not import an optional backend at module level",
          not offenders, "; ".join(offenders))


# --------------------------------------------------------------------------- 3

# Distribution name -> the top-level module it installs, where they differ.
DIST_TO_MODULE = {
    "opencv-python": "cv2",
    "pillow": "PIL",
    "pyyaml": "yaml",
    "uvicorn[standard]": "uvicorn",
    "python-multipart": "multipart",
}

# Third-party imports that requirements.txt deliberately does NOT name, each
# with the reason it is still guaranteed present at runtime.
EXEMPT_IMPORTS = {
    # fastapi IS declared and re-exports starlette; the app also imports
    # starlette directly for Middleware/Response types. Pinned here rather than
    # added to requirements so the fastapi/starlette version stays coupled.
    "starlette": "installed by fastapi, which is declared",
    # Installed from the PyTorch CPU wheel index by the Dockerfile and CI, on
    # purpose — declaring it in requirements.txt would pull the CUDA build.
    "torch": "separate wheel index; see Dockerfile",
    "torchvision": "separate wheel index; see Dockerfile",
    # Commented out in requirements.txt as optional extras. Guarded by
    # test_the_heavy_dependencies_are_imported_lazily above.
    "transformers": "optional: pose / grounded-sam2 backends",
    "sam2": "optional: grounded-sam2 backend, installed from git",
    "fiftyone": "optional: [label] extra",
}


def _declared_modules() -> set[str]:
    mods = set()
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if not line:
            continue
        name = line
        for sep in (">=", "==", "<=", "~=", ">", "<", "!="):
            name = name.split(sep)[0]
        name = name.strip().lower()
        mods.add(DIST_TO_MODULE.get(name, name.split("[")[0]))
    return mods


def test_every_third_party_import_is_a_declared_dependency():
    """An import that only works because something else happened to pull the
    package in is a dependency with no owner: it disappears the day that other
    package tightens its own requirements, and the failure lands at boot in
    production rather than in CI."""
    declared = _declared_modules()
    first_party = {"cownting", "tests"}
    stdlib = set(sys.stdlib_module_names)
    undeclared: dict[str, str] = {}

    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    continue
                mods = [node.module.split(".")[0]]
            for mod in mods:
                if (mod in stdlib or mod in first_party or mod in declared
                        or mod in EXEMPT_IMPORTS):
                    continue
                undeclared.setdefault(mod, f"{_rel(path)}:{node.lineno}")

    check("no import resolves only by accident",
          not undeclared,
          "; ".join(f"{m} at {w}" for m, w in sorted(undeclared.items())))


def test_the_exemption_list_has_not_gone_stale():
    """An exemption that no longer corresponds to a real import is a claim
    nobody is checking any more. Fail on it, so the list stays a description of
    the code rather than a wish about it."""
    seen = set()
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                seen |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                seen.add(node.module.split(".")[0])
    stale = sorted(set(EXEMPT_IMPORTS) - seen) + sorted(LAZY_ONLY - seen)
    check("every exempted / lazy-only package is still imported somewhere",
          not stale, f"stale entries: {stale}")


def main():
    test_every_subpackage_on_disk_is_declared_in_pyproject()
    test_the_console_script_entry_point_resolves()
    test_the_heavy_dependencies_are_imported_lazily()
    test_the_optional_backends_are_not_reachable_from_a_plain_boot()
    test_every_third_party_import_is_a_declared_dependency()
    test_the_exemption_list_has_not_gone_stale()
    print(f"\n{'ALL OK' if not _FAILED else f'{_FAILED} FAILED'}")
    sys.exit(1 if _FAILED else 0)


if __name__ == "__main__":
    main()
