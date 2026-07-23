#!/usr/bin/env python3
import subprocess, re, json, time, urllib.request, sys
WEBHOOK = open('/opt/cownting/alerts/webhook.url').read().strip()
CONTAINER = 'cownting-cownting-1'
UA = 'cownting-alerts/1.0 (+https://cownting.schutera.com)'
def send(content):
    body = json.dumps({"content": content[:1900]}).encode()
    req = urllib.request.Request(WEBHOOK, data=body,
                                 headers={'Content-Type': 'application/json', 'User-Agent': UA})
    try:
        urllib.request.urlopen(req, timeout=8)
    except Exception as e:
        print("discord send failed:", e, file=sys.stderr)
LOGIN_RE = re.compile(r'\[cownting\.alert\] LOGIN user=(\S+)')
_last = {}
def rate_ok(key, window=120):
    now = time.time()
    if now - _last.get(key, 0) < window:
        return False
    _last[key] = now
    return True
def is_err_end(line):
    return bool(line) and not line[0].isspace() and re.search(r'(Error|Exception)\b', line)
def run():
    p = subprocess.Popen(['docker', 'logs', '-f', '--tail', '0', CONTAINER],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    tb = []
    for line in p.stdout:
        line = line.rstrip('\n')
        m = LOGIN_RE.search(line)
        if m:
            send(f"\U0001F510 cownting login: **{m.group(1)}**"); continue
        if 'Traceback (most recent call last)' in line:
            tb = [line]; continue
        if tb:
            tb.append(line)
            if is_err_end(line):
                if rate_ok('tb:' + line[:200]):
                    send(f"⚠️ cownting error:\n```\n{line[:300]}\n```")
                tb = []
            elif len(tb) > 80:
                tb = []
            continue
        if line.startswith('INFO:'):
            continue
        if re.search(r'\b(ERROR|CRITICAL)\b', line) or re.search(r'(Permission denied|upload failed|failed to|could not)', line, re.I):
            if rate_ok('ln:' + line[:80]):
                send(f"⚠️ cownting: {line[:400]}")
    p.wait()
if __name__ == '__main__':
    send("✅ cownting alert watcher online")
    while True:
        try:
            run()
        except Exception as e:
            print("watcher loop error:", e, file=sys.stderr)
        time.sleep(3)
