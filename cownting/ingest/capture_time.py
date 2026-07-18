"""Resolve a video's capture time from the file itself.

Two in-file sources, tried in this order by the caller:

1. `read_container_time` — the MP4/QuickTime `mvhd` header creation_time. The
   principled source most cameras write. Brinno TLC300 zeroes it (creation AND
   modification = 0), so it returns None for that hardware.

2. `read_burned_timestamp` — the timestamp Brinno burns into the bottom bar
   ("TLC300 MM/DD/YYYY  HH:MM:SS"). For those cameras it is the ONLY place the
   real date and time survive, so we read it from the pixels via a tiny embedded
   glyph-template matcher (OpenCV only — no OCR engine, no dependency, no asset).
   The bar is a camera overlay, crisp even when the scene is fogged or dark, and
   the glyph cells are pixel-stable across cameras; templates were harvested from
   real TLC300 footage and auto-labelled from the time-lapse's known progression.
"""
from __future__ import annotations

import re
import struct
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

# MP4/QuickTime store times as seconds since 1904-01-01 (UTC).
_MP4_EPOCH = datetime(1904, 1, 1)


def read_container_time(video_path: str | Path, *, scan: int = 4_000_000) -> datetime | None:
    """Capture time from the MP4/QuickTime `mvhd` header, or None if not present.

    Reads the movie-header creation_time (falling back to its modification_time).
    Returns None when the field is zero (Brinno and many action cams) or no
    `mvhd` atom is found in the scanned prefix — the caller then falls back to the
    burned-in bar. Never raises for a merely unreadable/short header.
    """
    path = Path(video_path)
    try:
        with open(path, "rb") as f:
            data = f.read(scan)
    except OSError:
        return None
    i = data.find(b"mvhd")
    if i < 0:
        return None
    p = i + 4  # skip the atom tag; next byte is the version
    version = data[p] if p < len(data) else 0
    try:
        if version == 1:  # 64-bit times
            created, modified = struct.unpack(">QQ", data[p + 4:p + 20])
        else:             # version 0, 32-bit times
            created, modified = struct.unpack(">II", data[p + 4:p + 12])
    except struct.error:
        return None
    secs = created or modified
    if not secs:
        return None
    try:
        return _MP4_EPOCH + timedelta(seconds=secs)
    except OverflowError:
        return None


# --------------------------------------------------------------------------- burned-in bar
# Normalized glyph grid. Each template is a GH*GW bitmap packed MSB-first into hex
# (row-major). Harvested as the per-class median over real TLC300 frames spanning
# 06:00–14:00 so every digit 0–9 plus '/' and ':' is covered.
_GW, _GH = 12, 18
_TEMPLATES_HEX = {
    "/": "00300600600c00c0180180300700e00c0180180300300600600c00",
    "0": "1f83fc70e606e07c03c03c03cf3cf3c03c03c03e0760670e3fc1f8",
    "1": "3e0fe0c60060060060060060060060060060060060060060ffffff",
    "2": "7f8ffcc0600300300300300700600c0180780e0180300600ffffff",
    "3": "1f87fe4060030030030030061fc1fc00e00300300300380effe7f8",
    "4": "01c03c02c06c0cc0cc18c18c30c60c60cc0cffffff00c00c00c00c",
    "5": "7fe7fe6006006006007f87fc40e00700300300300300780effc7f8",
    "6": "0fc3fe302600600c00cf8dfef06e07c03c03c03c036077063fe1f8",
    "7": "ffffff00600600e00c00c0180180780700700e00e01e0180180300",
    "8": "1f87fe606c03c03c03c036063fc3fc606c03c03c03c03e077fe1f8",
    "9": "1f87fc60ee06c03c03c03c03e0760f7fb1f300300600640c7fc3f0",
    ":": "ffffffffffffffffff000000000000000000ffffffffffffffffff",
    "C": "0fe1ff381600600c00c00c00c00c00c00c00c006006003811ff0fe",
    "L": "c00c00c00c00c00c00c00c00c00c00c00c00c00c00c00c00ffffff",
    "T": "ffffff060060060060060060060060060060060060060060060060",
}

# The stamp within the classified glyph string: MM/DD/YYYY immediately followed by
# HH:MM:SS (glyph runs are joined with no separators; the model prefix falls away).
_STAMP_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})(\d{2}):(\d{2}):(\d{2})")

# Frames to try before giving up: the first is almost always clean, but a rare
# corrupt lead frame shouldn't sink it.
_FRAMES_TRIED = (0, 1, 2, 4, 8, 16)


def _unpack(hexstr: str) -> np.ndarray:
    val = int(hexstr, 16)
    bits = [(val >> i) & 1 for i in range(_GW * _GH - 1, -1, -1)]
    return np.array(bits, dtype=np.uint8).reshape(_GH, _GW)


_TEMPLATES = {ch: _unpack(h) for ch, h in _TEMPLATES_HEX.items()}


def _bar_mask(frame: np.ndarray) -> np.ndarray:
    """Boolean text mask of the black bottom bar (True = bright glyph pixel).

    Finds the bar by its signature: near-full-width dark rows at the bottom, so a
    bright scene above the bar (grass, sky) can't leak into glyph segmentation.
    """
    h, _ = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    bottom = gray[max(0, h - 70):h, :]
    dark_frac = (bottom < 40).mean(axis=1)
    rows = np.where(dark_frac > 0.6)[0]
    if rows.size == 0:
        raise ValueError("no dark timestamp bar found at the bottom of the frame")
    return bottom[rows.min():rows.max() + 1, :] > 150


def _col_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Column spans of contiguous glyph pixels (1px specks dropped as noise)."""
    col = mask.any(axis=0)
    runs: list[tuple[int, int]] = []
    i, n = 0, len(col)
    while i < n:
        if col[i]:
            j = i
            while j < n and col[j]:
                j += 1
            if j - i >= 2:
                runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def _norm(glyph: np.ndarray) -> np.ndarray:
    """Crop a glyph to its ink bbox and resample to the template grid."""
    ys = np.where(glyph.any(axis=1))[0]
    xs = np.where(glyph.any(axis=0))[0]
    g = glyph[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    return (cv2.resize(g.astype(np.uint8) * 255, (_GW, _GH),
                       interpolation=cv2.INTER_AREA) > 127).astype(np.uint8)


def _classify(glyph: np.ndarray) -> str:
    g = _norm(glyph)
    return max(_TEMPLATES, key=lambda ch: int((g == _TEMPLATES[ch]).sum()))


def _read_frame_stamp(frame: np.ndarray) -> datetime:
    mask = _bar_mask(frame)
    text = "".join(_classify(mask[:, a:b]) for a, b in _col_runs(mask))
    m = _STAMP_RE.search(text)
    if not m:
        raise ValueError(f"no MM/DD/YYYY HH:MM:SS stamp in bar text {text!r}")
    mo, d, y, h, mi, s = map(int, m.groups())
    return datetime(y, mo, d, h, mi, s)


def read_burned_timestamp(video_path: str | Path) -> datetime | None:
    """Capture datetime from the burned-in Brinno bar, or None if unreadable.

    Tries the first few frames (a rare corrupt lead frame shouldn't sink it) and
    returns None — never raises — when no frame's bottom bar parses (wrong camera
    model, cropped footage, overlay disabled), so the caller can fall through to
    asking the user rather than guessing a day.
    """
    path = str(video_path)
    if not Path(path).exists():
        return None
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None
    try:
        for fi in _FRAMES_TRIED:
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ok, frame = cap.read()
            if not ok:
                break
            try:
                return _read_frame_stamp(frame)
            except ValueError:
                continue
        return None
    finally:
        cap.release()
