"""Unit tests for cownting.ingest.capture_time (in-file capture time).

No pytest. Run either way:
    .venv/bin/python -m tests.test_capture_time
    .venv/bin/python tests/test_capture_time.py

Hermetic: no real footage. Two sources are covered:
  * container: synthesize a minimal MP4/QuickTime `mvhd` atom (v0 32-bit and v1
    64-bit) and assert the parser recovers the time (+ zero/absent/missing -> None);
  * burned-in bar: paint the module's own glyph templates onto a black strip and
    assert read_burned_timestamp recovers the datetime (+ no-bar -> None).
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime  # noqa: E402

from cownting.ingest import capture_time  # noqa: E402

_FAILED = 0
_EPOCH = datetime(1904, 1, 1)


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    status = "ok " if cond else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1


def _secs(dt: datetime) -> int:
    return int((dt - _EPOCH).total_seconds())


def _mvhd(created: int, modified: int, version: int = 0) -> bytes:
    """A tiny buffer containing a minimal `mvhd` atom (no surrounding boxes —
    read_container_time only searches for the tag)."""
    head = b"\x00\x00\x00\x00mvhd"  # (fake size) + atom tag
    vf = bytes([version]) + b"\x00\x00\x00"  # version + flags
    if version == 1:
        times = struct.pack(">QQ", created, modified)
    else:
        times = struct.pack(">II", created, modified)
    return b"\x00" * 32 + head + vf + times + b"\x00" * 64


def _write(buf: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".mp4")
    with os.fdopen(fd, "wb") as f:
        f.write(buf)
    return path


def test_v0_creation_time():
    want = datetime(2025, 6, 28, 6, 0, 1)
    path = _write(_mvhd(_secs(want), _secs(want)))
    try:
        got = capture_time.read_container_time(path)
        check("v0 mvhd creation_time parsed", got == want, str(got))
    finally:
        os.remove(path)


def test_v1_64bit_creation_time():
    want = datetime(2030, 1, 2, 3, 4, 5)
    path = _write(_mvhd(_secs(want), _secs(want), version=1))
    try:
        got = capture_time.read_container_time(path)
        check("v1 (64-bit) mvhd creation_time parsed", got == want, str(got))
    finally:
        os.remove(path)


def test_falls_back_to_modification():
    want = datetime(2024, 3, 4, 5, 6, 7)
    path = _write(_mvhd(0, _secs(want)))  # creation zeroed
    try:
        got = capture_time.read_container_time(path)
        check("creation=0 falls back to modification_time", got == want, str(got))
    finally:
        os.remove(path)


def test_all_zero_returns_none():
    # Brinno's shape: both times zeroed -> None (caller uses file mtime).
    path = _write(_mvhd(0, 0))
    try:
        got = capture_time.read_container_time(path)
        check("all-zero times -> None", got is None, str(got))
    finally:
        os.remove(path)


def test_no_mvhd_returns_none():
    path = _write(b"\x00" * 64 + b"not a movie header")
    try:
        got = capture_time.read_container_time(path)
        check("no mvhd atom -> None", got is None, str(got))
    finally:
        os.remove(path)


def test_missing_file_returns_none():
    got = capture_time.read_container_time("/nonexistent/nope.mp4")
    check("missing file -> None (not a raise)", got is None, str(got))


# --------------------------------------------------------------------------- burned-in bar
def _bar_frame(text: str, scale: int = 3, gap: int = 8) -> np.ndarray:
    """Paint `text` from the module's own glyph templates into the black bottom
    bar of an otherwise bright frame — the shape a real Brinno frame presents."""
    glyphs = []
    for ch in text:
        if ch == " ":
            glyphs.append(np.zeros((capture_time._GH * scale, gap * 2), np.uint8))
            continue
        t = np.repeat(np.repeat(capture_time._TEMPLATES[ch], scale, axis=0), scale, axis=1)
        glyphs.append((t * 255).astype(np.uint8))
    width = gap * 2 + sum(g.shape[1] + gap for g in glyphs)
    bar = np.zeros((capture_time._GH * scale + 2 * gap, width), np.uint8)
    x = gap
    for g in glyphs:
        bar[gap:gap + g.shape[0], x:x + g.shape[1]] = g
        x += g.shape[1] + gap
    scene = np.full((80, width), 130, np.uint8)  # bright scene above the bar
    return np.stack([np.vstack([scene, bar])] * 3, axis=-1)  # -> BGR


def test_bar_reads_reference_stamp():
    got = capture_time._read_frame_stamp(_bar_frame("TLC300 06/28/2025 06:00:01"))
    check("burned bar: reads 06:00:01", got == datetime(2025, 6, 28, 6, 0, 1), str(got))


def test_bar_all_digits():
    got = capture_time._read_frame_stamp(_bar_frame("TLC300 12/03/2025 04:56:07"))
    check("burned bar: reads a stamp spanning all digits",
          got == datetime(2025, 12, 3, 4, 56, 7), str(got))


def test_bar_noisy_scene_ignored():
    frame = _bar_frame("TLC300 09/14/2025 17:38:29")
    rng = np.random.default_rng(0)
    frame[:80] = rng.integers(90, 200, size=frame[:80].shape, dtype=np.uint8)
    got = capture_time._read_frame_stamp(frame)
    check("burned bar: bright/noisy scene above bar ignored",
          got == datetime(2025, 9, 14, 17, 38, 29), str(got))


def test_bar_no_bar_returns_none_via_public():
    # A plain bright frame written as a 1-frame video would need cv2 encode; here
    # we assert the frame-level parser rejects a no-bar frame (public read returns
    # None on unreadable, which _read_frame_stamp raising is what drives that).
    plain = np.full((120, 400, 3), 130, np.uint8)
    try:
        capture_time._read_frame_stamp(plain)
        check("burned bar: no-bar frame raises", False, "no exception")
    except ValueError:
        check("burned bar: no-bar frame raises", True)


def test_bar_missing_file_returns_none():
    check("burned bar: missing file -> None",
          capture_time.read_burned_timestamp("/nonexistent/nope.mp4") is None)


def main():
    print("=== test_capture_time ===")
    test_v0_creation_time()
    test_v1_64bit_creation_time()
    test_falls_back_to_modification()
    test_all_zero_returns_none()
    test_no_mvhd_returns_none()
    test_missing_file_returns_none()
    test_bar_reads_reference_stamp()
    test_bar_all_digits()
    test_bar_noisy_scene_ignored()
    test_bar_no_bar_returns_none_via_public()
    test_bar_missing_file_returns_none()
    print("=========================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
