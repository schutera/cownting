"""Every alert the app prints must actually reach someone.

`alerts/watch.py` runs on the host, tails `docker logs -f`, and forwards
interesting lines to a Discord webhook. It is the ONLY path by which a failure in
an unattended background job — the weekly label backup above all — becomes
something a human learns about. The app prints `[cownting.alert] …` lines
believing that is enough.

It was not. The watcher had one regex per alert family (`LOGIN`, `LABEL-
MILESTONE`) plus a generic catch-all matching `\\b(ERROR|CRITICAL)\\b` — case
SENSITIVE — or the phrases `Permission denied|upload failed|failed to|could not`.
`[cownting.alert] LABEL-BACKUP failed: IO Error: Cannot open file …` matches
none of them: "Error" is not "ERROR", and "failed:" is not "failed to". So every
backup failure, every staleness warning and every oversize warning was printed
and dropped. The weekly backup could stop being delivered for months and the
only evidence would be in a log nobody reads.

The routing is now a pure function, and this file is the thing that stops it
drifting again: it SCANS THE SOURCE for every `[cownting.alert]` string the app
can emit and asserts the watcher forwards each one. A new alert tag added to the
app without a thought for delivery fails here rather than in six months' silence.

No pytest. Run either way:
    py -3.11 -m tests.test_alerts
    py -3.11 tests/test_alerts.py
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = pathlib.Path(__file__).resolve().parent.parent

_FAILED = 0


def _ascii(text: str) -> str:
    """The forwarded messages carry emoji, and this suite runs on a cp1252
    Windows console where printing one is a UnicodeEncodeError — a crash in the
    reporting, dressed up as a test failure."""
    return text.encode("ascii", "backslashreplace").decode("ascii")


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    line = f"[{'ok ' if cond else 'FAIL'}] {_ascii(name)}"
    if detail:
        line += f"  ({_ascii(detail)})"
    print(line)
    if not cond:
        _FAILED += 1
        if os.environ.get("PYTEST_CURRENT_TEST"):
            raise AssertionError(line)


def _watch():
    """`alerts/` is not a package — load the module by path."""
    spec = importlib.util.spec_from_file_location(
        "_alerts_watch", ROOT / "alerts" / "watch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


watch = _watch()

# Every tag the app emits, with a REAL example line for each — taken from the
# actual format strings, not invented. A tag added to the app and not to this
# list is caught by test_every_alert_tag_in_the_source_is_covered below.
# tag -> (a real example line, a substring the forwarded message MUST carry).
# The second half is what stops a rule from "forwarding" a line as a generic
# "something happened" that tells the reader nothing actionable.
SAMPLES: dict[str, tuple[str, str]] = {
    # api.py:525
    "LOGIN": ("[cownting.alert] LOGIN user=mark", "mark"),
    # api.py:546 — found by this test's own source scan, which is the point of it
    "ACT-AS": ("[cownting.alert] ACT-AS user=mark role=user", "role=user"),
    # labels_backup.py:101
    "LABEL-MILESTONE": (
        "[cownting.alert] LABEL-MILESTONE 1500 labeled=1512 trigger=schedule", "1,500"),
    # labels_backup.py:93
    "LABEL-BACKUP": (
        '[cownting.alert] LABEL-BACKUP failed run 42: IO Error: Cannot open file '
        '"/app/data/labels.duckdb"', "failed run 42"),
    # config.py — a typo'd key is ignored by pydantic, so this line is the only
    # sign that the config file says something the app is not doing
    "CONFIG": ("[cownting.alert] CONFIG unknown key 'paths.panels' in "
               "config/cownting.prod.yaml — ignored, so its value has no effect",
               "paths.panels"),
    # labels_backup.py:109
    "ATTRS-SCORE": (
        "[cownting.alert] ATTRS-SCORE enqueued job 7 dataset=all rescore=False",
        "job 7"),
}

# Other real LABEL-BACKUP shapes: each was dropped by the old rules.
BACKUP_SHAPES = [
    "[cownting.alert] LABEL-BACKUP failed: Catalog write-write conflict on create",
    "[cownting.alert] LABEL-BACKUP delivery failed run 42: rejected as too large",
    ("[cownting.alert] LABEL-BACKUP stale: last successful backup 2026-08-01, "
     "over 14 days ago"),
    "[cownting.alert] LABEL-BACKUP oversize: full is 9000000 B (cap 8000000 B)",
    "[cownting.alert] LABEL-BACKUP prune: could not remove labels-x.zip",
    "[cownting.alert] LABEL-MILESTONE post failed: HTTP Error 500",
]


def test_every_tagged_alert_is_forwarded():
    for tag, (line, must_carry) in SAMPLES.items():
        hit = watch.classify(line)
        check(f"{tag} is forwarded", hit is not None, "DROPPED - nobody is told")
        if hit:
            # An alert that reaches the channel saying only "something happened"
            # is an alert nobody can act on, so the rendered text must carry the
            # detail from the line that makes it actionable.
            check(f"{tag}'s message carries {must_carry!r}",
                  must_carry in hit[0], hit[0])


def test_every_backup_failure_shape_is_forwarded():
    """The whole family, because the backup is the job whose silent failure is
    most expensive: nothing else notices that labels have stopped leaving the
    box."""
    for line in BACKUP_SHAPES:
        hit = watch.classify(line)
        check(f"forwarded: {line[26:70]}", hit is not None, "DROPPED")


def test_a_milestone_is_never_rate_limited():
    """The app prints each milestone exactly once. Suppressing a repeat of a
    once-only message does not delay it — it destroys it."""
    hit = watch.classify(SAMPLES["LABEL-MILESTONE"][0])
    check("a milestone is sent unconditionally", hit is not None and hit[1] is None,
          str(hit))
    rate = watch._Rate()
    sent = sum(1 for _ in range(3) if rate.ok(hit[1]))
    check("...so three ticks send three times", sent == 3, str(sent))


def test_a_repeated_failure_is_rate_limited():
    """The opposite case: a failing job can print the same line every tick, and
    the channel must not become unreadable."""
    hit = watch.classify(SAMPLES["LABEL-BACKUP"][0])
    check("a backup failure carries a rate key", hit is not None and hit[1] is not None,
          str(hit))
    rate = watch._Rate()
    sent = sum(1 for _ in range(5) if rate.ok(hit[1]))
    check("...so five identical lines send once", sent == 1, str(sent))


def test_the_rate_limiter_is_bounded():
    """This process is meant to run for months. The old suppressor was a plain
    dict keyed on the log line and never evicted anything."""
    rate = watch._Rate(cap=64)
    for i in range(500):
        rate.ok(f"key-{i}")
    check("the suppressor does not grow without bound",
          len(rate._seen) <= 64, f"{len(rate._seen)} entries after 500 distinct keys")


def test_ordinary_log_noise_is_not_forwarded():
    """A watcher that forwards everything is a watcher nobody reads."""
    for line in ("INFO:     127.0.0.1:52344 - \"GET /api/site HTTP/1.1\" 200 OK",
                 "[cownting.pipeline] segment: 120/1440 frames",
                 "",
                 "  File \"/app/cownting/api.py\", line 12, in get_site"):
        check(f"ignored: {line[:48]!r}", watch.classify(line) is None,
              str(watch.classify(line)))


def test_every_alert_tag_in_the_source_is_covered():
    """THE ANTI-DRIFT CHECK, and the reason this file is worth its length.

    Scans cownting/** for every `[cownting.alert] TAG` literal the app can
    print and asserts two things: this test knows about the tag, and the watcher
    forwards it. Adding an alert to the app without arranging for its delivery
    now fails here — which is the failure the LABEL-BACKUP family had, silently,
    for its whole life."""
    tags: dict[str, str] = {}
    pat = re.compile(r"\[cownting\.alert\]\s+(?:\{[^}]*\}\s*)?([A-Z][A-Z-]{2,})")
    for path in sorted((ROOT / "cownting").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for m in pat.finditer(text):
            tags.setdefault(m.group(1), f"{path.relative_to(ROOT).as_posix()}")

    check("the source actually emits tagged alerts (the scan is not vacuous)",
          len(tags) >= 3, f"found {sorted(tags)}")

    unknown = {t: w for t, w in tags.items() if t not in SAMPLES}
    check("every alert tag in the source has a sample here",
          not unknown,
          "; ".join(f"{t} at {w}" for t, w in sorted(unknown.items())))

    dropped = []
    for tag in tags:
        sample = SAMPLES.get(tag)
        line = sample[0] if sample else f"[cownting.alert] {tag} something went wrong"
        if watch.classify(line) is None:
            dropped.append(tag)
    check("every alert tag in the source is forwarded by the watcher",
          not dropped, f"DROPPED: {dropped}")


def test_an_unknown_future_tag_is_still_forwarded():
    """The rule is open on purpose. A tag nobody has written a case for is
    exactly the one whose delivery would otherwise be forgotten."""
    hit = watch.classify("[cownting.alert] SOME-NEW-THING it broke")
    check("an unrecognised tag is forwarded anyway", hit is not None, str(hit))


def main():
    test_every_tagged_alert_is_forwarded()
    test_every_backup_failure_shape_is_forwarded()
    test_a_milestone_is_never_rate_limited()
    test_a_repeated_failure_is_rate_limited()
    test_the_rate_limiter_is_bounded()
    test_ordinary_log_noise_is_not_forwarded()
    test_every_alert_tag_in_the_source_is_covered()
    test_an_unknown_future_tag_is_still_forwarded()
    print(f"\n{'ALL OK' if not _FAILED else f'{_FAILED} FAILED'}")
    sys.exit(1 if _FAILED else 0)


if __name__ == "__main__":
    main()
