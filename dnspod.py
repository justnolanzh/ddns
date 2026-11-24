#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DNSPod DDNS updater — keeps one A record pointed at this host's public IP.

Auth   : DNSPod API 2.0 token read from TOKEN_FILE (chmod 600, never inline).
Target : ddns.json (domain_id / record_id / sub_domain), re-read every cycle so
         renaming or re-pointing the record needs no daemon restart.
Run    : python3 dnspod.py            # daemon loop (what the LaunchDaemon does)
         python3 dnspod.py --once     # one check, write if IP changed
         python3 dnspod.py --dry-run  # one check, never write
"""

import fcntl
import http.client
import ipaddress
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, ".dnspod_token")
CONFIG_FILE = os.path.join(BASE_DIR, "ddns.json")
LOG_DIR = os.environ.get("DDNS_LOG_DIR", os.path.join(BASE_DIR, "logs"))
LOG_FILE = os.path.join(LOG_DIR, "ddns.log")
LOCK_FILE = os.environ.get("DDNS_LOCK", os.path.join(BASE_DIR, ".dnspod.lock"))

DEFAULT_TARGET = {
    "domain": "example.com",
    "domain_id": 12345678,
    "record_id": 1234567890,
    "sub_domain": "ddns",
    "record_line": "默认",
    "ttl": "600",
}

INTERVAL = 60          # seconds between checks
TIMEOUT = 10           # seconds per network call
MAX_BACKOFF = 1800
IP_SOURCES = (
    "https://ifconfig.me/ip",
    "https://api.ipify.org",
    "https://ipinfo.io/ip",
)

log = logging.getLogger("ddns")


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as exc:                     # log dir not writable -> stderr only
        print("warning: cannot write %s (%s)" % (LOG_FILE, exc), file=sys.stderr)
    if sys.stderr.isatty():                    # keep launchd's stdout/stderr file quiet
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def read_token():
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError as exc:
        raise SystemExit("cannot read token file %s: %s" % (TOKEN_FILE, exc))
    if "," not in token:
        raise SystemExit("token in %s must be 'ID,Token'" % TOKEN_FILE)
    return token


def load_target():
    """Merge ddns.json over DEFAULT_TARGET. Re-read each cycle on purpose."""
    target = dict(DEFAULT_TARGET)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
    except FileNotFoundError:
        return target
    except (OSError, ValueError) as exc:
        log.error("cannot parse %s (%s); using built-in target", CONFIG_FILE, exc)
        return target
    for key in ("domain", "sub_domain", "record_line", "ttl"):
        if key in loaded:
            target[key] = str(loaded[key])
    for key in ("domain_id", "record_id"):
        if key in loaded:
            try:
                target[key] = int(loaded[key])
            except (TypeError, ValueError):
                log.error("ignoring non-numeric %s in %s", key, CONFIG_FILE)
    return target


def fqdn(target):
    sub = target["sub_domain"]
    return target["domain"] if sub in ("@", "") else "%s.%s" % (sub, target["domain"])


def api(path, payload):
    """POST to DNSPod API 2.0, return parsed JSON. Raises on transport errors."""
    body = urllib.parse.urlencode(payload)
    conn = http.client.HTTPSConnection("dnsapi.cn", timeout=TIMEOUT)
    try:
        conn.request("POST", path, body, {
            "Content-type": "application/x-www-form-urlencoded",
            "Accept": "text/json",
            "User-Agent": "ddns-updater/2.0",
        })
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        if resp.status != 200:
            raise RuntimeError("HTTP %s from %s: %r" % (resp.status, path, raw[:200]))
        return json.loads(raw)
    finally:
        conn.close()


def current_record(token, target):
    data = api("/Record.Info", {
        "login_token": token, "format": "json",
        "domain_id": target["domain_id"], "record_id": target["record_id"],
    })
    code = str(data.get("status", {}).get("code"))
    if code != "1":
        raise RuntimeError("Record.Info failed: %s %s" % (code, data.get("status", {}).get("message")))
    return data.get("record", {})


def _probe(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ddns-updater/2.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        text = resp.read(256).decode("utf-8", "replace")
    candidate = text.split()[0].strip() if text.split() else ""
    return str(ipaddress.IPv4Address(candidate))       # raises for garbage / IPv6


def detect_ip():
    """Ask several sources, trust the majority. Returns an IPv4 string or None."""
    votes, errors = Counter(), []
    for url in IP_SOURCES:
        try:
            votes[_probe(url)] += 1
        except Exception as exc:                        # noqa: BLE001 - any failure is just a lost vote
            errors.append("%s: %s" % (url, exc))
    if not votes:
        log.error("all IP sources failed: %s", "; ".join(errors))
        return None
    ip, count = votes.most_common(1)[0]
    if count == 1 and len(votes) > 1:
        log.warning("IP sources disagree (%s); using %s from a single source",
                    dict(votes), ip)
    return ip


def update_record(token, target, ip):
    data = api("/Record.Ddns", {
        "login_token": token, "format": "json",
        "domain_id": target["domain_id"], "record_id": target["record_id"],
        "sub_domain": target["sub_domain"], "record_line": target["record_line"],
        "record_type": "A", "ttl": target["ttl"], "value": ip,
    })
    status = data.get("status", {})
    code, message = str(status.get("code")), status.get("message")
    if code != "1":
        log.error("Record.Ddns rejected for %s (code %s: %s)", fqdn(target), code, message)
        return False
    log.info("record updated -> %s %s (dnspod: %s)", fqdn(target), ip, message)
    return True


def check_once(token, dry_run=False):
    """One cycle. Returns True when the wanted IP is in place (or no action needed)."""
    target = load_target()
    ip = detect_ip()
    if not ip:
        return False
    try:
        record = current_record(token, target)
    except Exception as exc:                            # noqa: BLE001
        log.error("cannot read record %s: %s", fqdn(target), exc)
        return False
    live = record.get("value")
    if live == ip:
        log.info("no change: %s record=%s actual=%s", fqdn(target), live, ip)
        return True
    log.info("IP change detected for %s: record=%s actual=%s", fqdn(target), live, ip)
    if dry_run:
        log.info("dry-run: would update %s -> %s", fqdn(target), ip)
        return False
    return update_record(token, target, ip)


def main():
    setup_logging()
    args = set(sys.argv[1:])
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    dry_run = "--dry-run" in args
    once = "--once" in args or dry_run

    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("another instance is already running (%s)", LOCK_FILE)
        return 1

    token = read_token()
    target = load_target()
    log.info("started: %s (domain %s, record %s), interval %ss%s",
             fqdn(target), target["domain_id"], target["record_id"], INTERVAL,
             ", once" if once else "")

    if once:
        return 0 if check_once(token, dry_run=dry_run) else 1

    failures = 0
    while True:
        try:
            ok = check_once(token)
            failures = 0 if ok else failures + 1
        except Exception as exc:                        # noqa: BLE001 - keep the daemon alive
            failures += 1
            log.error("cycle failed: %s", exc)
        delay = INTERVAL if failures == 0 else min(INTERVAL * (2 ** failures), MAX_BACKOFF)
        if failures:
            log.warning("backing off %ss after %s consecutive failure(s)", delay, failures)
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
