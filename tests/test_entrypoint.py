"""Container-boot self-heal tests: entrypoint.sh must re-own root-owned strays
in the /app/data bind mount and drop to the unprivileged app user before the
server starts (the fix for the production PermissionError on saving areas).

Static checks always run (script wiring, LF endings, Dockerfile hookup). The
functional check boots the real base image with a planted root-owned file and
asserts it comes out re-owned and writable — it needs a Docker daemon, so it is
skipped (not failed) where Docker is unavailable, e.g. inside the container's
own pre-boot gate. CI (ubuntu-latest) has Docker, so the full path runs there.

No pytest. Run either way:
    .venv/bin/python -m tests.test_entrypoint
    .venv/bin/python tests/test_entrypoint.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_FAILED = 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRYPOINT = os.path.join(ROOT, "entrypoint.sh")
DOCKERFILE = os.path.join(ROOT, "Dockerfile")

# Must match the runtime stage's base image in the Dockerfile — the functional
# test boots the same userland the production entrypoint runs in.
BASE_IMAGE = "python:3.11-slim-bookworm"
APP_UID = "10001"


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    status = "ok " if cond else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1
        # A check that only prints is invisible to pytest: the file reports
        # green while assertions inside it fail. `python -m tests` (the
        # pre-boot gate) counts them, but nothing else does.
        if os.environ.get("PYTEST_CURRENT_TEST"):
            raise AssertionError(line)


def test_script_static():
    """The script itself: present, LF-only, POSIX shebang, and the moving parts
    (conditional chown, privilege drop, plain exec fallback) all in place."""
    check("entrypoint.sh exists", os.path.isfile(ENTRYPOINT))
    if not os.path.isfile(ENTRYPOINT):
        return
    raw = open(ENTRYPOINT, "rb").read()
    check("no CRLF endings (breaks /bin/sh)", b"\r" not in raw)
    text = raw.decode()
    check("POSIX shebang", text.startswith("#!/bin/sh"))
    check("fails fast (set -eu)", "set -eu" in text)
    check("re-owns only wrong-owned files", "! -user cownting" in text)
    check("drops privileges via setpriv", "setpriv" in text and "--reuid=cownting" in text)
    check("execs (not spawns) the server", "exec setpriv" in text)
    check("non-root fallback still runs the CMD", 'exec "$@"' in text)


def test_dockerfile_wiring():
    """The Dockerfile must actually arm the self-heal: copy the script, use it as
    the single ENTRYPOINT, and NOT set USER (a USER directive would start the
    entrypoint unprivileged, silently disabling the chown while everything else
    still appears to work)."""
    text = open(DOCKERFILE).read()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    entrypoints = [ln for ln in lines if ln.startswith("ENTRYPOINT")]
    check("exactly one ENTRYPOINT", len(entrypoints) == 1, str(entrypoints))
    check("ENTRYPOINT runs entrypoint.sh",
          bool(entrypoints) and "entrypoint.sh" in entrypoints[0], str(entrypoints[:1]))
    check("entrypoint.sh is copied into the image",
          any(ln.startswith("COPY") and "entrypoint.sh" in ln for ln in lines))
    check("no USER directive (root at boot, dropped by the entrypoint)",
          not any(ln.startswith("USER ") for ln in lines),
          "; ".join(ln for ln in lines if ln.startswith("USER ")))
    check("app uid in Dockerfile matches the test's expectation",
          f"--uid {APP_UID} " in text, f"expected --uid {APP_UID}")

    # The build context must include the script: a .dockerignore line like
    # "*.sh" would make the COPY fail only at build time — catch it here.
    di = os.path.join(ROOT, ".dockerignore")
    if os.path.isfile(di):
        patterns = [ln.strip() for ln in open(di) if ln.strip() and not ln.startswith("#")]
        offenders = [p for p in patterns if p in ("entrypoint.sh", "*.sh", "/entrypoint.sh")]
        check("entrypoint.sh not dockerignored", not offenders, str(offenders))


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=15
        ).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def test_boot_self_heal():
    """End to end in the real base image: plant a root-owned file where Janine's
    save crashed (data/areas/<day>), run the entrypoint, and assert the server
    command comes up as the app user with the stray re-owned and the dir
    writable."""
    if not _docker_ready():
        print("[skip] docker unavailable — functional entrypoint check not run "
              "(static checks above still cover the wiring)")
        return

    inner = (
        "useradd --create-home --uid {uid} cownting && "
        "mkdir -p /app/data/areas/2025-10-15 && "
        "touch /app/data/areas/2025-10-15/rootfile.json && "
        "sh /app/entrypoint.sh sh -c "
        "'echo UID=$(id -u) GID=$(id -g); "
        "stat -c OWNER=%u:%g /app/data/areas/2025-10-15/rootfile.json; "
        "touch /app/data/areas/2025-10-15/new.json && echo WRITABLE'"
    ).format(uid=APP_UID)
    proc = subprocess.run(
        ["docker", "run", "--rm",
         "-v", f"{ENTRYPOINT}:/app/entrypoint.sh:ro",
         BASE_IMAGE, "sh", "-c", inner],
        capture_output=True, text=True, timeout=300,
    )
    out = proc.stdout
    check("container run succeeds", proc.returncode == 0,
          (proc.stderr or out).strip()[-200:])
    check("server runs as the app user", f"UID={APP_UID} GID={APP_UID}" in out, out.strip())
    check("root-owned stray re-owned", f"OWNER={APP_UID}:{APP_UID}" in out, out.strip())
    check("app user can write beside it", "WRITABLE" in out, out.strip())


def test_uid_matches_ownership_fix():
    """The uid the entrypoint heals to must be the uid the app actually runs as —
    the Dockerfile is the single source; regions.save_count_areas and every other
    writer inherit it. Guard the constant used across this module against a
    silent Dockerfile change."""
    text = open(DOCKERFILE).read()
    m = re.search(r"--uid\s+(\d+)\s+cownting", text)
    check("Dockerfile declares the cownting uid", m is not None)
    if m:
        check("test constants track the Dockerfile uid", m.group(1) == APP_UID,
              f"Dockerfile={m.group(1)} test={APP_UID}")


def main():
    print("=== test_entrypoint ===")
    test_script_static()
    test_dockerfile_wiring()
    test_uid_matches_ownership_fix()
    test_boot_self_heal()
    print("=================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
