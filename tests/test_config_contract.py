"""The shipped example config must describe the config the app actually reads.

Pydantic IGNORES unknown keys by default. That makes a config typo the quietest
class of bug there is: the file says one thing, the app does another, and nothing
anywhere disagrees out loud. `config/cownting.example.yaml` is what an operator
copies and edits, so a wrong key in it is a wrong key in every deployment
derived from it.

Two were shipped:

  paths.panels:  data/panels.json    # the field is `panel_areas` — setting this
                                     # changed nothing at all
  shade:                             # not a field on any model; the whole block
    enabled: false                   # was inert, including the comment claiming
    margin_px: 0                     # it governs the shelter test

...and three keys appeared TWICE inside `annotation:` (`min_instance_px`,
`clear_instance_px`, `small_share`), where PyYAML silently keeps the last. An
operator tuning the first copy — the one under the "Which crops get served
first" heading, with the explanatory prose — would have seen no effect whatever.

`Config.load` now prints a `[cownting.alert] CONFIG unknown key …` line for each
such key (a warning, not `extra="forbid"`: refusing to construct would turn a
stale key in a long-lived deployment into a container that will not boot,
discovered mid-deploy). These tests keep the example itself clean.

No pytest. Run either way:
    py -3.11 -m tests.test_config_contract
    py -3.11 tests/test_config_contract.py
"""
from __future__ import annotations

import collections
import contextlib
import io as _io
import os
import pathlib
import re
import sys
import tempfile

import yaml
from pydantic import BaseModel

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting.config import Config  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config" / "cownting.example.yaml"

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


def _model_fields(model, prefix: str = "") -> dict:
    out = {}
    for name, field in model.model_fields.items():
        key = f"{prefix}{name}"
        out[key] = field
        ann = field.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            out.update(_model_fields(ann, key + "."))
    return out


def _yaml_keys(node, prefix: str = "") -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            out.add(f"{prefix}{k}")
            if isinstance(v, dict):
                out |= _yaml_keys(v, f"{prefix}{k}.")
    return out


def _example() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def test_every_key_in_the_example_is_a_real_field():
    """THE DRIFT CHECK. A key the models do not define is a line of the config
    file that does nothing — and reads, to whoever edits it, exactly like a line
    that does something."""
    known = set(_model_fields(Config))
    unknown = sorted(_yaml_keys(_example()) - known)
    check("no key in the shipped example is silently ignored",
          not unknown, f"not fields: {unknown}")


def test_the_example_has_no_duplicate_keys():
    """PyYAML keeps the LAST of two keys in one mapping, silently. Three of these
    shipped inside `annotation:`, so the documented copy — the one with the
    reasoning above it — was the dead one."""
    lines = EXAMPLE.read_text(encoding="utf-8").splitlines()
    block, seen = None, collections.defaultdict(list)
    for i, raw in enumerate(lines, 1):
        if re.match(r"^[A-Za-z_]", raw):
            block = raw.split(":")[0]
        m = re.match(r"^  ([A-Za-z_][A-Za-z0-9_]*):", raw)
        if m and block:
            seen[f"{block}.{m.group(1)}"].append(i)
    dups = {k: v for k, v in seen.items() if len(v) > 1}
    check("no key appears twice in one block",
          not dups, "; ".join(f"{k} at lines {v}" for k, v in sorted(dups.items())))


def test_the_example_actually_loads():
    """It is the file operators copy. If it does not construct a Config, the
    first thing anyone does with this project fails."""
    try:
        cfg = Config(**_example())
        err = ""
    except Exception as e:  # noqa: BLE001 — the failure IS the finding
        cfg, err = None, f"{type(e).__name__}: {e}"
    check("config/cownting.example.yaml constructs a Config", not err, err)
    if cfg is not None:
        check("...and the renamed path field took effect",
              cfg.paths.panel_areas.endswith("panel_areas.json"), cfg.paths.panel_areas)


def test_every_top_level_section_is_documented():
    """A whole section missing from the example is a feature an operator cannot
    discover from the file they were told to copy. `auth:` in particular is what
    `.env.example` tells you to edit for HTTPS."""
    top_model = {k for k in _model_fields(Config) if "." not in k}
    top_example = {k for k in _yaml_keys(_example()) if "." not in k}
    # `dataset:` is genuinely optional (ids are derived from capture time) and
    # `pose:` is an off-by-default experimental stage; both are documented in
    # config.py rather than here. Named explicitly so the exemption is a decision
    # and not an oversight.
    exempt = {"dataset", "pose"}
    missing = sorted(top_model - top_example - exempt)
    check("every top-level config section appears in the example",
          not missing, f"undocumented: {missing}")


def _load_with_output(text: str) -> tuple[object, str]:
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "c.yaml")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                cfg = Config.load(p)
            except Exception as e:  # noqa: BLE001
                cfg = e
        return cfg, buf.getvalue()


_MINIMAL = "cameras:\n  - id: camera_01\n    video: a.mp4\n"


def test_an_unknown_key_is_reported_and_not_fatal():
    cfg, out = _load_with_output(
        _MINIMAL + "paths:\n  panels: data/panels.json\nshade:\n  enabled: true\n")
    check("a typo'd nested key is reported", "paths.panels" in out, repr(out))
    check("an unknown top-level block is reported", "'shade'" in out, repr(out))
    check("...as a line alerts/watch.py forwards",
          "[cownting.alert] CONFIG" in out, repr(out))
    check("...and loading still succeeds, so a stale key cannot block a deploy",
          isinstance(cfg, Config), type(cfg).__name__)


def test_a_correct_config_is_silent():
    """A warning that fires on a healthy config is a warning people learn to
    ignore — which would take the two real ones down with it."""
    _, out = _load_with_output(_MINIMAL + "annotation:\n  uncertain_below: 0.5\n")
    check("a clean config prints nothing", out.strip() == "", repr(out))
    _, out2 = _load_with_output(EXAMPLE.read_text(encoding="utf-8"))
    check("...and neither does the shipped example", out2.strip() == "", repr(out2))


def test_an_empty_or_malformed_config_says_so():
    """`cls(**data)` on a None (an all-comments file) is `TypeError: argument
    after ** must be a mapping` — a stack trace where a sentence would do."""
    cfg, _ = _load_with_output("# nothing but a comment\n")
    check("an empty config file names the problem",
          isinstance(cfg, ValueError) and "empty" in str(cfg), repr(cfg))
    cfg2, _ = _load_with_output("- not\n- a mapping\n")
    check("a non-mapping config file names the problem",
          isinstance(cfg2, ValueError) and "mapping" in str(cfg2), repr(cfg2))


def main():
    test_every_key_in_the_example_is_a_real_field()
    test_the_example_has_no_duplicate_keys()
    test_the_example_actually_loads()
    test_every_top_level_section_is_documented()
    test_an_unknown_key_is_reported_and_not_fatal()
    test_a_correct_config_is_silent()
    test_an_empty_or_malformed_config_says_so()
    print(f"\n{'ALL OK' if not _FAILED else f'{_FAILED} FAILED'}")
    sys.exit(1 if _FAILED else 0)


if __name__ == "__main__":
    main()
