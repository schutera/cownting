#!/usr/bin/env python3
"""Forward interesting container log lines to a Discord webhook.

Runs on the host, outside the container, tailing `docker logs -f`. That is
deliberate: it must survive the app crashing, so it cannot live inside it.

THE ROUTING IS A PURE FUNCTION (`classify`) on purpose. It used to be inline in
the read loop, which meant the only way to know whether a given alert actually
reached anyone was to trigger it in production and look at the channel — and the
answer, for every `[cownting.alert] LABEL-BACKUP …` line the app emits, was no.
The generic catch-all matches `\\b(ERROR|CRITICAL)\\b` case-SENSITIVELY, so
"LABEL-BACKUP failed: IO Error" matched neither that nor the `failed to|could
not` alternation, and the weekly backup could stop being delivered indefinitely
with the failure printed to a log nobody reads. `tests/test_alerts.py` now scans
the source for every `[cownting.alert]` line the app can print and asserts this
function forwards each one.

Usage:  watch.py [container-name]
Env:    COWNTING_ALERT_WEBHOOK   the webhook URL (preferred)
        COWNTING_ALERT_CONTAINER the container to tail
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

UA = "cownting-alerts/1.0"

# Tagged alerts the app prints on purpose. ANY `[cownting.alert] TAG rest…` line
# is forwarded — a positive, open rule rather than one regex per tag, so a tag
# added to the app tomorrow is delivered without anyone remembering this file.
# The two tags below are singled out only to render them nicely.
ALERT_RE = re.compile(r"\[cownting\.alert\] ([A-Z][A-Z-]*) (.*)")
LOGIN_RE = re.compile(r"\[cownting\.alert\] LOGIN user=(\S+)")
MILESTONE_RE = re.compile(r"\[cownting\.alert\] LABEL-MILESTONE (\d+) labeled=(\d+)")

# Untagged lines worth forwarding. `(?i)` on the phrase alternation only — an
# uppercase-ERROR match must stay case-sensitive or every "Error" in ordinary
# prose is an alert.
GENERIC_RE = re.compile(r"\b(ERROR|CRITICAL)\b")
PHRASE_RE = re.compile(r"(Permission denied|upload failed|failed to|could not)", re.I)

# Tags whose message the app promises to print exactly ONCE per event. Rate
# limiting those would not delay the message, it would destroy it.
_ONCE_ONLY = {"LABEL-MILESTONE"}


def classify(line):
    """(text_to_send, rate_key) for a log line, or None to ignore it.

    `rate_key` None means "send unconditionally"; otherwise the caller drops
    repeats of that key inside the rate window.
    """
    if not line or line.startswith("INFO:"):
        return None

    m = LOGIN_RE.search(line)
    if m:
        return f"\U0001F510 cownting login: **{m.group(1)}**", None

    m = MILESTONE_RE.search(line)
    if m:
        level, labeled = int(m.group(1)), int(m.group(2))
        return (f"\U0001F404 cownting label milestone: **{level:,} instances "
                f"labeled** ({labeled:,} in the store)"), None

    m = ALERT_RE.search(line)
    if m:
        tag, rest = m.group(1), m.group(2)
        key = None if tag in _ONCE_ONLY else f"alert:{tag}:{rest[:80]}"
        return f"⚠️ cownting {tag}: {rest[:400]}", key

    if GENERIC_RE.search(line) or PHRASE_RE.search(line):
        return f"⚠️ cownting: {line[:400]}", "ln:" + line[:80]
    return None


def is_err_end(line):
    return bool(line) and not line[0].isspace() and re.search(r"(Error|Exception)\b", line)


def _webhook():
    url = os.environ.get("COWNTING_ALERT_WEBHOOK")
    if url:
        return url.strip()
    path = os.environ.get("COWNTING_ALERT_WEBHOOK_FILE",
                          "/opt/cownting/alerts/webhook.url")
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError as e:
        sys.exit(f"watch.py: no webhook — set COWNTING_ALERT_WEBHOOK or fix {path} ({e})")


class _Rate:
    """Bounded repeat-suppressor. The old one was a plain dict keyed on the line,
    which grew for the life of the process — and this process is meant to run for
    months."""

    def __init__(self, window=120, cap=512):
        self.window, self.cap, self._seen = window, cap, {}

    def ok(self, key):
        if key is None:
            return True
        now = time.time()
        if now - self._seen.get(key, 0) < self.window:
            return False
        self._seen[key] = now
        if len(self._seen) > self.cap:
            for k in sorted(self._seen, key=self._seen.get)[: self.cap // 2]:
                del self._seen[k]
        return True


def send(webhook, content):
    body = json.dumps({"content": content[:1900]}).encode()
    req = urllib.request.Request(
        webhook, data=body,
        headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        urllib.request.urlopen(req, timeout=8)
    except Exception as e:  # noqa: BLE001 — the watcher must outlive a bad send
        print("discord send failed:", e, file=sys.stderr)


def run(webhook, container, rate):
    p = subprocess.Popen(["docker", "logs", "-f", "--tail", "0", container],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1)
    tb = []
    for line in p.stdout:
        line = line.rstrip("\n")
        if "Traceback (most recent call last)" in line:
            tb = [line]
            continue
        if tb:
            tb.append(line)
            if is_err_end(line):
                if rate.ok("tb:" + line[:200]):
                    send(webhook, f"⚠️ cownting error:\n```\n{line[:300]}\n```")
                tb = []
            elif len(tb) > 80:
                tb = []
            continue
        hit = classify(line)
        if hit and rate.ok(hit[1]):
            send(webhook, hit[0])
    return p.wait()


def main(argv):
    webhook = _webhook()
    container = (argv[1] if len(argv) > 1
                 else os.environ.get("COWNTING_ALERT_CONTAINER", "cownting-cownting-1"))
    rate = _Rate()
    send(webhook, f"✅ cownting alert watcher online ({container})")
    fails = 0
    while True:
        try:
            code = run(webhook, container, rate)
        except Exception as e:  # noqa: BLE001
            print("watcher loop error:", e, file=sys.stderr)
            code = -1
        # A container that does not exist makes `docker logs` exit immediately.
        # The old loop retried that silently every 3 s forever, having already
        # announced itself as healthy — so a renamed deploy directory turned the
        # watcher into a spinning no-op that claimed to be watching.
        fails = fails + 1 if code != 0 else 0
        if fails == 5:
            send(webhook, f"⚠️ cownting alert watcher cannot read logs for "
                          f"`{container}` (docker logs exited {code}) — ALERTS ARE OFF")
        time.sleep(3)


if __name__ == "__main__":
    main(sys.argv)
