"""Auth + user-management tests: the login gate blocks un-authenticated /api
calls, the bootstrap admin can sign in and manage users, non-admins are kept out
of the admin routes, the full poweruser surface (POWERUSER_SURFACE) is exercised
as a role x route matrix, a policy scan asserts every mutating /api route
carries a role gate, and the store refuses to orphan the last admin.

No pytest. Run either way:
    .venv/bin/python -m tests.test_auth
    .venv/bin/python tests/test_auth.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cownting import auth, db  # noqa: E402
from cownting.api import create_app  # noqa: E402
from cownting.config import AuthCfg, CameraCfg, Config, PathsCfg  # noqa: E402

_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    status = "ok " if cond else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1


def _app(d: str):
    dbp = os.path.join(d, "cownting.duckdb")
    con = db.connect(dbp)
    db.init_db(con)
    con.close()
    # Deterministic bootstrap admin + signing key for the test.
    os.environ["COWNTING_SECRET"] = "test-secret-not-for-prod"
    os.environ["COWNTING_ADMIN_USER"] = "admin"
    os.environ["COWNTING_ADMIN_PASSWORD"] = "s3cret"
    config = Config(
        cameras=[CameraCfg(id="camera_01", video="unused.mp4")],
        # labels_db_path/backups_dir MUST be redirected into the temp dir:
        # create_app boots the label store unconditionally, so a default PathsCfg
        # would have every test in this file write the real repo's
        # data/labels.duckdb and leave annotation rows behind.
        paths=PathsCfg(db_path=dbp, count_areas=os.path.join(d, "areas.json"),
                       labels_db_path=os.path.join(d, "labels.duckdb"),
                       backups_dir=os.path.join(d, "backups")),
        auth=AuthCfg(enabled=True),
    )
    return create_app(config), dbp


def test_password_hash_roundtrip():
    h = auth.hash_password("hunter2")
    check("hash is self-describing scrypt", h.startswith("scrypt$"), h.split("$")[0])
    check("correct password verifies", auth.verify_password("hunter2", h))
    check("wrong password rejected", not auth.verify_password("nope", h))
    check("garbage hash rejected (no crash)", not auth.verify_password("x", "not-a-hash"))


def test_gate_and_admin_flow():
    with tempfile.TemporaryDirectory() as d:
        app, _dbp = _app(d)

        # Unauthenticated: protected route is blocked, public probe says "no".
        anon = TestClient(app)
        r = anon.get("/api/site")
        check("anon GET /api/site -> 401", r.status_code == 401, str(r.status_code))
        r = anon.get("/api/me")
        check("anon GET /api/me -> 401", r.status_code == 401, str(r.status_code))

        # Wrong credentials rejected.
        r = anon.post("/api/login", json={"username": "admin", "password": "wrong"})
        check("bad login -> 401", r.status_code == 401, str(r.status_code))

        # Bootstrap admin signs in; the session cookie now rides on the client.
        admin = TestClient(app)
        r = admin.post("/api/login", json={"username": "admin", "password": "s3cret"})
        check("admin login -> 200", r.status_code == 200, str(r.status_code))
        check("admin login echoes role", r.json().get("role") == "admin", str(r.json()))
        r = admin.get("/api/me")
        check("admin /api/me -> admin", r.status_code == 200 and r.json().get("role") == "admin", str(r.json()))
        r = admin.get("/api/site")
        check("admin can reach /api/site", r.status_code == 200, str(r.status_code))

        # Admin creates a plain user.
        r = admin.post("/api/admin/users", json={"username": "bob", "password": "pw", "role": "user"})
        check("create user -> 200", r.status_code == 200, str(r.status_code))
        names = {u["username"] for u in r.json().get("users", [])}
        check("user list now has admin + bob", names == {"admin", "bob"}, str(names))

        # Duplicate username rejected.
        r = admin.post("/api/admin/users", json={"username": "bob", "password": "pw", "role": "user"})
        check("duplicate user -> 400", r.status_code == 400, str(r.status_code))

        # The new user can log in but is NOT allowed into the admin routes.
        bob = TestClient(app)
        r = bob.post("/api/login", json={"username": "bob", "password": "pw"})
        check("bob login -> 200", r.status_code == 200, str(r.status_code))
        r = bob.get("/api/site")
        check("bob can reach the dashboard", r.status_code == 200, str(r.status_code))
        r = bob.get("/api/admin/users")
        check("bob blocked from admin -> 403", r.status_code == 403, str(r.status_code))
        r = bob.post("/api/admin/users", json={"username": "eve", "password": "pw", "role": "user"})
        check("bob cannot create users -> 403", r.status_code == 403, str(r.status_code))

        # Admin resets bob's password; old password stops working, new one works.
        r = admin.patch("/api/admin/users/bob", json={"password": "newpw"})
        check("reset password -> 200", r.status_code == 200, str(r.status_code))
        r = TestClient(app).post("/api/login", json={"username": "bob", "password": "pw"})
        check("old password now rejected", r.status_code == 401, str(r.status_code))
        r = TestClient(app).post("/api/login", json={"username": "bob", "password": "newpw"})
        check("new password accepted", r.status_code == 200, str(r.status_code))

        # Promote bob to admin, then the last-admin guard: admin still can't be
        # left without any admin.
        r = admin.patch("/api/admin/users/bob", json={"role": "admin"})
        check("promote bob -> 200", r.status_code == 200, str(r.status_code))

        # Delete admin's own account is refused (can't lock yourself out mid-session).
        r = admin.delete("/api/admin/users/admin")
        check("admin cannot delete self -> 400", r.status_code == 400, str(r.status_code))

        # Delete bob (now admin) is fine — admin remains.
        r = admin.delete("/api/admin/users/bob")
        check("delete bob -> 200", r.status_code == 200, str(r.status_code))

        # Now admin is the last admin: demoting it is refused by the store guard.
        r = admin.patch("/api/admin/users/admin", json={"role": "user"})
        check("cannot demote last admin -> 400", r.status_code == 400, str(r.status_code))

        # Logout clears the session.
        r = admin.post("/api/logout")
        check("logout -> 200", r.status_code == 200, str(r.status_code))
        r = admin.get("/api/site")
        check("after logout /api/site -> 401", r.status_code == 401, str(r.status_code))


# Every route a poweruser may touch — the full data-management surface. The
# targets are fictitious ("nope"), so a passed gate shows up as an ordinary
# 4xx (404/400/422), never 403, and leaves no data behind.
POWERUSER_SURFACE = [
    ("POST",   "/api/uploads", None),                                  # add a new day
    ("GET",    "/api/export.csv", None),                               # download data
    ("DELETE", "/api/datasets/nope?confirm=x", None),                  # delete a day
    ("GET",    "/api/dataset/nope/camera-health", None),               # per-camera health
    ("DELETE", "/api/dataset/nope/camera/cam", None),                  # drop one stream
    ("POST",   "/api/dataset/nope/camera", None),                      # add/replace a stream
    ("POST",   "/api/dataset/nope/camera/cam/clip",
     {"start": "2025-10-15T08:00:00", "end": "2025-10-15T09:00:00"}),  # cut stream to a window
    ("POST",   "/api/dataset/nope/camera/cam/restore", None),          # undo the cut
    ("POST",   "/api/areas", {"areas": {}}),                           # save count areas
    ("POST",   "/api/panel-areas", {"areas": {}}),                     # save panel areas
    ("POST",   "/api/localize", None),                                 # recount placements
    # The six label-taxonomy mutations (M3 §4.1, frozen paths). Powerusers curate
    # the questions and answers; plain annotators may only answer them.
    ("POST",   "/api/label/groups", None),                             # add a question
    ("PATCH",  "/api/label/groups/nope", {"name": "x"}),               # edit/archive one
    ("POST",   "/api/label/groups/nope/move", {"dir": "up"}),          # reorder questions
    ("POST",   "/api/label/groups/nope/classes", None),                # add an answer option
    ("PATCH",  "/api/label/classes/nope.x", {"name": "x"}),            # edit/archive an option
    ("POST",   "/api/label/classes/nope.x/move", {"dir": "up"}),       # reorder options
    ("GET",    "/api/labels/backup/status", None),                     # weekly-zip status
    ("POST",   "/api/labels/backup/run", {"force": False}),            # run it by hand
]


# The label WRITE surface. Distinct from POWERUSER_SURFACE because labeling is the
# one mutation a plain `user` is meant to perform — require_labeler admits every
# known role. The instance_key is forged, so submit/skip fail the anchor check with
# a 400 and undo no-ops: passing the gate never stores an annotation.
_ANCHOR = {"dataset_id": None, "camera_id": "camera_01", "frame_file": "00000001.jpg",
           "bbox": [1.0, 2.0, 3.0, 4.0], "ordinal": 0}
LABELER_SURFACE = [
    ("POST", "/api/label/submit", {"instance_key": "0" * 32, "anchor": _ANCHOR,
                                   "answers": {}, "taxonomy_revision": 0}),
    ("POST", "/api/label/skip",   {"instance_key": "0" * 32, "anchor": _ANCHOR,
                                   "reason": "occluded"}),
    ("POST", "/api/label/undo",   {"instance_key": "0" * 32}),
    ("POST", "/api/label/events", {"session_id": "s", "kind": "session_start"}),
]


def test_labeler_gate():
    """A plain `user` may label but may not curate the taxonomy.

    That split is the whole point of the Label page's permission model, and it is
    the one place in the app where a viewer writes rows — so it gets its own
    matrix rather than riding on POWERUSER_SURFACE."""
    with tempfile.TemporaryDirectory() as d:
        app, _dbp = _app(d)
        admin = TestClient(app)
        admin.post("/api/login", json={"username": "admin", "password": "s3cret"})
        admin.post("/api/admin/users", json={"username": "viewer", "password": "pw", "role": "user"})

        viewer = TestClient(app)
        viewer.post("/api/login", json={"username": "viewer", "password": "pw"})

        for method, url, body in LABELER_SURFACE:
            r = viewer.request(method, url, json=body)
            check(f"user {method} {url} passes require_labeler (not 403)",
                  r.status_code != 403, str(r.status_code))
        # Specifically 400 (bad anchor), not 403 — proving the gate let it through
        # and the handler, not the gate, is what rejected the forged key.
        r = viewer.post("/api/label/submit", json=LABELER_SURFACE[0][2])
        check("user submit with a forged anchor -> 400", r.status_code == 400, str(r.status_code))

        r = viewer.get("/api/label/taxonomy")
        check("user can read the taxonomy -> 200", r.status_code == 200, str(r.status_code))
        r = viewer.patch("/api/label/classes/sun_exposure.shaded", json={"name": "x"})
        check("user cannot edit the taxonomy -> 403", r.status_code == 403, str(r.status_code))

        # An admin previewing `user` keeps the same split: can label, cannot curate.
        admin.post("/api/act-as", json={"role": "user"})
        r = admin.post("/api/label/submit", json=LABELER_SURFACE[0][2])
        check("acting user can label (400 not 403)", r.status_code == 400, str(r.status_code))
        r = admin.patch("/api/label/classes/sun_exposure.shaded", json={"name": "x"})
        check("acting user cannot edit the taxonomy -> 403", r.status_code == 403, str(r.status_code))
        admin.post("/api/act-as", json={"role": "admin"})

        # And the app-wide login gate still answers first for a stranger.
        anon = TestClient(app)
        r = anon.post("/api/label/submit", json=LABELER_SURFACE[0][2])
        check("anonymous label submit -> 401", r.status_code == 401, str(r.status_code))


def test_poweruser_data_gate():
    """Role x route matrix over the full poweruser surface: every entry is 403
    for a plain `user`, past the gate (anything but 403) for a real poweruser,
    and an admin acting as poweruser mirrors the real poweruser exactly."""
    with tempfile.TemporaryDirectory() as d:
        app, _dbp = _app(d)

        admin = TestClient(app)
        admin.post("/api/login", json={"username": "admin", "password": "s3cret"})
        # One of each non-admin role.
        admin.post("/api/admin/users", json={"username": "pow", "password": "pw", "role": "poweruser"})
        admin.post("/api/admin/users", json={"username": "viewer", "password": "pw", "role": "user"})

        pow_ = TestClient(app)
        pow_.post("/api/login", json={"username": "pow", "password": "pw"})
        viewer = TestClient(app)
        viewer.post("/api/login", json={"username": "viewer", "password": "pw"})

        # The gate runs before the handler, so a plain user is exactly 403 on
        # every entry; a poweruser gets past the gate and the fictitious target
        # then fails with a normal not-found/validation error (i.e. NOT 403).
        for method, url, body in POWERUSER_SURFACE:
            r = viewer.request(method, url, json=body)
            check(f"user {method} {url} -> 403", r.status_code == 403, str(r.status_code))
            r = pow_.request(method, url, json=body)
            check(f"poweruser {method} {url} passes gate (not 403)",
                  r.status_code != 403, str(r.status_code))

        # An admin previewing the poweruser role must mirror a real poweruser
        # exactly: the same non-403 across the whole surface.
        admin.post("/api/act-as", json={"role": "poweruser"})
        for method, url, body in POWERUSER_SURFACE:
            r = admin.request(method, url, json=body)
            check(f"acting poweruser {method} {url} passes gate (not 403)",
                  r.status_code != 403, str(r.status_code))
        r = admin.post("/api/act-as", json={"role": "admin"})
        check("switch back to admin -> 200", r.status_code == 200, str(r.status_code))

        # Powerusers are still NOT admins — user management and the act-as
        # preview stay admin-only.
        check("poweruser blocked from admin routes -> 403",
              pow_.get("/api/admin/users").status_code == 403)
        check("poweruser cannot act-as -> 403",
              pow_.post("/api/act-as", json={"role": "user"}).status_code == 403)

        # POST /api/areas + /api/panel-areas kicked off a background localize
        # worker that opens the DuckDB file; on Windows the TemporaryDirectory
        # cleanup would crash while that thread still holds it, so drain first.
        deadline = time.time() + 10
        while time.time() < deadline:
            if pow_.get("/api/localize/status").json().get("status") in ("idle", "done", "failed"):
                break
            time.sleep(0.1)


def test_every_mutating_route_is_gated():
    """Policy scan over the live route table: every mutating /api route must
    carry an explicit role gate (require_poweruser or require_admin), except the
    session handshake routes that gate internally. A new mutating endpoint
    added without a gate fails here — that is the point."""
    with tempfile.TemporaryDirectory() as d:
        app, _dbp = _app(d)
        MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
        SELF_GATING = {"/api/login", "/api/logout", "/api/act-as"}
        missing = []
        for route in app.routes:
            if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
                continue
            if not (route.methods & MUTATING) or route.path in SELF_GATING:
                continue
            gates = {dep.call.__name__ for dep in route.dependant.dependencies if dep.call}
            # require_labeler joined the accepted set with the Label page: labeling
            # is the one mutation a plain `user` may perform, and the gate still
            # re-derives the effective role exactly like the other two — a real
            # gate, not a bypass. Listing the label routes in SELF_GATING instead
            # would permanently exempt every FUTURE label mutation from this scan.
            if not gates & {"require_poweruser", "require_admin", "require_labeler"}:
                missing.append(f"{'/'.join(sorted(route.methods & MUTATING))} {route.path}")
        check("every mutating /api route carries a role gate", not missing, "; ".join(missing))


def test_act_as_role_preview():
    """An admin can switch the session's EFFECTIVE role to preview the app as a
    lower role (gates genuinely 403), and can always switch back because act-as
    itself is gated on the REAL role."""
    with tempfile.TemporaryDirectory() as d:
        app, _dbp = _app(d)
        admin = TestClient(app)
        admin.post("/api/login", json={"username": "admin", "password": "s3cret"})
        admin.post("/api/admin/users", json={"username": "pow", "password": "pw", "role": "poweruser"})

        # Only a real admin may act-as at all.
        pow_ = TestClient(app)
        pow_.post("/api/login", json={"username": "pow", "password": "pw"})
        r = pow_.post("/api/act-as", json={"role": "user"})
        check("poweruser cannot act-as -> 403", r.status_code == 403, str(r.status_code))

        # Acting as a plain user: view works, data + admin routes genuinely 403,
        # and /api/me reports both the effective and the real role.
        r = admin.post("/api/act-as", json={"role": "user"})
        check("act-as user -> 200", r.status_code == 200, str(r.status_code))
        me = admin.get("/api/me").json()
        check("me: effective user, real admin",
              me.get("role") == "user" and me.get("real_role") == "admin", str(me))
        check("acting user: dashboard still viewable", admin.get("/api/site").status_code == 200)
        check("acting user: export blocked -> 403", admin.get("/api/export.csv").status_code == 403)
        check("acting user: admin routes blocked -> 403", admin.get("/api/admin/users").status_code == 403)

        # Acting as poweruser: data allowed, admin still blocked.
        admin.post("/api/act-as", json={"role": "poweruser"})
        check("acting poweruser: export allowed", admin.get("/api/export.csv").status_code == 200)
        check("acting poweruser: admin routes blocked -> 403",
              admin.get("/api/admin/users").status_code == 403)

        # The way back: gated on the REAL role, so it works even while acting.
        r = admin.post("/api/act-as", json={"role": "admin"})
        check("switch back -> 200", r.status_code == 200, str(r.status_code))
        check("admin routes reachable again", admin.get("/api/admin/users").status_code == 200)

        # Bogus role rejected; a fresh login always starts at the real role.
        r = admin.post("/api/act-as", json={"role": "root"})
        check("bogus role -> 400", r.status_code == 400, str(r.status_code))
        admin.post("/api/act-as", json={"role": "user"})
        admin.post("/api/login", json={"username": "admin", "password": "s3cret"})
        me = admin.get("/api/me").json()
        check("fresh login clears the preview", me.get("role") == "admin", str(me))


def test_auth_disabled_is_open():
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, "cownting.duckdb")
        con = db.connect(dbp)
        db.init_db(con)
        con.close()
        config = Config(
            cameras=[CameraCfg(id="camera_01", video="unused.mp4")],
            paths=PathsCfg(db_path=dbp, count_areas=os.path.join(d, "areas.json"),
                           labels_db_path=os.path.join(d, "labels.duckdb"),
                           backups_dir=os.path.join(d, "backups")),
            auth=AuthCfg(enabled=False),
        )
        client = TestClient(create_app(config))
        r = client.get("/api/site")
        check("auth disabled: /api/site open -> 200", r.status_code == 200, str(r.status_code))
        r = client.get("/api/me")
        check("auth disabled: /api/me reports synthetic admin",
              r.status_code == 200 and r.json().get("auth_disabled") is True, str(r.json()))


def main():
    print("=== test_auth ===")
    test_password_hash_roundtrip()
    test_gate_and_admin_flow()
    test_poweruser_data_gate()
    test_labeler_gate()
    test_every_mutating_route_is_gated()
    test_act_as_role_preview()
    test_auth_disabled_is_open()
    print("=================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
