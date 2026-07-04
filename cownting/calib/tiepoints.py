"""Site-wide cross-camera tie points for joint calibration.

A tie point is the SAME physical ground feature sighted in two or more cameras.
Each is a list of observations ``[{"camera": id, "pt": [x, y]}, ...]`` in that
camera's reference-image pixels. They carry no known orthophoto position — the
joint solver treats each as a free bundle-adjustment landmark whose location is
the multi-camera consensus, which couples the cameras' calibrations (see
joint.py). Pure-python / JSON only.
"""
from __future__ import annotations

import json
from pathlib import Path


def load_tiepoints(path: str) -> list:
    """Return the tie points ``[[{"camera","pt"}, ...], ...]`` or [] if unset/corrupt."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.load(open(p)).get("tiepoints")
    except Exception:  # noqa: BLE001 - a corrupt file must not break joint calibration
        return []
    return data if isinstance(data, list) else []


def save_tiepoints(path: str, tiepoints) -> None:
    """Persist tie points, keeping only well-formed observations (>= 2 cameras each)."""
    clean = []
    for tp in tiepoints or []:
        obs = [
            {"camera": str(o["camera"]), "pt": [float(o["pt"][0]), float(o["pt"][1])]}
            for o in tp
            if isinstance(o, dict) and "camera" in o and "pt" in o
        ]
        if len({o["camera"] for o in obs}) >= 2:  # needs >= 2 distinct cameras
            clean.append(obs)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"tiepoints": clean}, f, indent=2)
