"""Lightweight user store + password hashing for the dashboard's login.

Deliberately minimal: a single `users` table in the same DuckDB, passwords
hashed with stdlib scrypt (no extra dependency), and role-based access with three
roles — `admin` (manage users + everything below), `poweruser` (upload, download,
and delete data), and `user` (view the dashboard only). The session cookie itself
is handled by Starlette's SessionMiddleware in api.py; this module only owns the
credential store.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from typing import Literal, Optional, TypedDict

import duckdb

Role = Literal["admin", "poweruser", "user"]
ROLES: tuple[Role, ...] = ("admin", "poweruser", "user")

# Roles allowed to manage data (upload / download / delete). Admins are a
# superset. Kept here so the API gate and any CLI checks share one definition.
DATA_ROLES: frozenset[str] = frozenset({"admin", "poweruser"})


def can_manage_data(role: str | None) -> bool:
    """True if `role` may upload, download, or delete data."""
    return role in DATA_ROLES

# scrypt work factors (RFC 7914). N must be a power of two; these are the
# interactive-login defaults and hash in a few ms on a laptop.
_N, _R, _P, _DKLEN = 2**14, 8, 1, 32

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


class UserRow(TypedDict):
    username: str
    role: Role


def valid_username(name: str) -> bool:
    return bool(_USERNAME_RE.fullmatch(name))


# --------------------------------------------------------------------- hashing
def hash_password(password: str) -> str:
    """Salted scrypt hash, self-describing so verify never needs the params.

    Format: `scrypt$<N>$<r>$<p>$<salt_hex>$<hash_hex>`."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of `password` against a `hash_password` string."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(hash_hex) // 2,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


# ----------------------------------------------------------------------- store
def init_auth(con: duckdb.DuckDBPyConnection) -> None:
    """Create the users table if absent. Idempotent (safe on every boot)."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            username   VARCHAR PRIMARY KEY,
            pw_hash    VARCHAR NOT NULL,
            role       VARCHAR NOT NULL DEFAULT 'user',
            created_at TIMESTAMP DEFAULT now()
        );
        """
    )


def list_users(con: duckdb.DuckDBPyConnection) -> list[UserRow]:
    rows = con.execute(
        "SELECT username, role FROM users ORDER BY username"
    ).fetchall()
    return [{"username": u, "role": r} for u, r in rows]


def get_user(con: duckdb.DuckDBPyConnection, username: str) -> Optional[dict]:
    row = con.execute(
        "SELECT username, pw_hash, role FROM users WHERE username = ?", [username]
    ).fetchone()
    if not row:
        return None
    return {"username": row[0], "pw_hash": row[1], "role": row[2]}


def user_exists(con: duckdb.DuckDBPyConnection, username: str) -> bool:
    return con.execute(
        "SELECT 1 FROM users WHERE username = ?", [username]
    ).fetchone() is not None


def count_admins(con: duckdb.DuckDBPyConnection) -> int:
    return con.execute(
        "SELECT count(*) FROM users WHERE role = 'admin'"
    ).fetchone()[0]


def authenticate(con: duckdb.DuckDBPyConnection, username: str, password: str) -> Optional[UserRow]:
    """Return the user on a correct password, else None."""
    u = get_user(con, username)
    if u and verify_password(password, u["pw_hash"]):
        return {"username": u["username"], "role": u["role"]}
    return None


def create_user(con: duckdb.DuckDBPyConnection, username: str, password: str, role: Role = "user") -> None:
    if not valid_username(username):
        raise ValueError("username must be 1-32 chars of letters, digits, _ . or -")
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if user_exists(con, username):
        raise ValueError(f"user {username!r} already exists")
    con.execute(
        "INSERT INTO users (username, pw_hash, role) VALUES (?, ?, ?)",
        [username, hash_password(password), role],
    )


def set_password(con: duckdb.DuckDBPyConnection, username: str, password: str) -> None:
    if not user_exists(con, username):
        raise ValueError(f"unknown user {username!r}")
    con.execute(
        "UPDATE users SET pw_hash = ? WHERE username = ?",
        [hash_password(password), username],
    )


def set_role(con: duckdb.DuckDBPyConnection, username: str, role: Role) -> None:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if not user_exists(con, username):
        raise ValueError(f"unknown user {username!r}")
    # Never orphan the instance: refuse to demote the last remaining admin.
    if role != "admin":
        u = get_user(con, username)
        if u and u["role"] == "admin" and count_admins(con) <= 1:
            raise ValueError("cannot demote the last admin")
    con.execute("UPDATE users SET role = ? WHERE username = ?", [role, username])


def delete_user(con: duckdb.DuckDBPyConnection, username: str) -> None:
    u = get_user(con, username)
    if not u:
        raise ValueError(f"unknown user {username!r}")
    if u["role"] == "admin" and count_admins(con) <= 1:
        raise ValueError("cannot delete the last admin")
    con.execute("DELETE FROM users WHERE username = ?", [username])


def ensure_bootstrap_admin(con: duckdb.DuckDBPyConnection) -> Optional[str]:
    """Guarantee at least one admin so a fresh DB is reachable.

    If the users table is empty, seed one admin from `COWNTING_ADMIN_USER` /
    `COWNTING_ADMIN_PASSWORD` (defaults `admin` / `admin`). Returns a warning
    string when a default/derived credential was used (so the caller can print
    it), or None when nothing was created."""
    if list_users(con):
        return None
    username = os.environ.get("COWNTING_ADMIN_USER", "admin").strip() or "admin"
    password = os.environ.get("COWNTING_ADMIN_PASSWORD", "").strip()
    warn_default = not password
    if not password:
        password = "admin"
    create_user(con, username, password, role="admin")
    if warn_default:
        return (
            f"created bootstrap admin {username!r} with the DEFAULT password "
            "'admin' — change it on the Admin page (or set COWNTING_ADMIN_PASSWORD "
            "before first boot)."
        )
    return f"created bootstrap admin {username!r} from COWNTING_ADMIN_* env vars."
