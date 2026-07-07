"""One-command test runner: `python -m tests`.

Discovers every tests/test_*.py, runs each as its own `python -m tests.<mod>`
(process isolation — each module owns its `_FAILED`/`sys.exit`), prints a
PASS/FAIL line per module, and exits non-zero if any module failed. This is what
`cownting serve` runs as a pre-boot gate.

    python -m tests            # run all
    python -m tests test_api   # run a subset (by module name, with/without .py)
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Starlette's TestClient prints a deprecation warning on import — drop it so a
# green run is actually quiet.
_NOISE = ("StarletteDeprecationWarning", "from starlette.testclient")


def _discover() -> list[str]:
    return sorted(
        os.path.basename(p)[:-3] for p in glob.glob(os.path.join(HERE, "test_*.py"))
    )


def _clean(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not any(n in ln for n in _NOISE))


def _run(mod: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", f"tests.{mod}"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    return proc.returncode == 0, _clean(proc.stdout + proc.stderr)


def main(argv: list[str]) -> int:
    wanted = [a[:-3] if a.endswith(".py") else a for a in argv]
    mods = [m for m in _discover() if not wanted or m in wanted]
    if not mods:
        print(f"no test modules matched {wanted or '(all)'}", file=sys.stderr)
        return 1

    failures: list[str] = []
    for mod in mods:
        ok, output = _run(mod)
        print(f"[{'PASS' if ok else 'FAIL'}] tests.{mod}")
        if not ok:
            failures.append(mod)
            print(output.rstrip() + "\n")

    print("-" * 40)
    print(f"{len(mods) - len(failures)}/{len(mods)} modules passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
