"""Auth + user-management tests: the login gate blocks un-authenticated /api
calls, the bootstrap admin can sign in and manage users, non-admins are kept out
of the admin routes, and the store refuses to orphan the last admin.

No pytest. Run either way:
    .venv/bin/python -m tests.test_auth
    .venv/bin/python tests/test_auth.py
"""
from __future__ import annotations

import os
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
        paths=PathsCfg(db_path=dbp, count_areas=os.path.join(d, "areas.json")),
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


def test_poweruser_data_gate():
    """Data-management routes (export / upload / delete) are open to admin and
    poweruser but blocked for plain `user` accounts."""
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

        # CSV export: admin + poweruser allowed, plain user forbidden.
        check("admin can export CSV", admin.get("/api/export.csv").status_code == 200)
        check("poweruser can export CSV", pow_.get("/api/export.csv").status_code == 200)
        check("plain user blocked from export -> 403",
              viewer.get("/api/export.csv").status_code == 403)

        # Delete a day: the gate runs before the handler, so a plain user is 403
        # regardless of whether the dataset exists; poweruser gets past the gate
        # (then a normal not-found/confirm error, i.e. NOT 403).
        r = viewer.delete("/api/datasets/nope?confirm=x")
        check("plain user blocked from delete -> 403", r.status_code == 403, str(r.status_code))
        r = pow_.delete("/api/datasets/nope?confirm=x")
        check("poweruser passes delete gate (not 403)", r.status_code != 403, str(r.status_code))

        # Upload: plain user rejected at the gate (multipart body not even needed).
        r = viewer.post("/api/uploads")
        check("plain user blocked from upload -> 403", r.status_code == 403, str(r.status_code))

        # Powerusers are still NOT admins — user management stays admin-only.
        check("poweruser blocked from admin routes -> 403",
              pow_.get("/api/admin/users").status_code == 403)


def test_auth_disabled_is_open():
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, "cownting.duckdb")
        con = db.connect(dbp)
        db.init_db(con)
        con.close()
        config = Config(
            cameras=[CameraCfg(id="camera_01", video="unused.mp4")],
            paths=PathsCfg(db_path=dbp, count_areas=os.path.join(d, "areas.json")),
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
    test_auth_disabled_is_open()
    print("=================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
