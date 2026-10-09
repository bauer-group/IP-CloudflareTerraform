#!/usr/bin/env python3
"""Control and assertions for the mock Cloudflare API (runs inside cf-mock).

    mockctl.py health                       compose healthcheck
    mockctl.py seed --marker M              add the marker TXT record (content M)
    mockctl.py probe-auth                   prove the API rejects bad credentials
    mockctl.py tamper --marker M            change the record out of band
    mockctl.py check --marker M --expect present|absent

The test scripts run it with `docker compose exec -T cf-mock python
/mock/mockctl.py ...`; the marker is passed as an argument, never pasted into
code. `check` asserts the record state and, from the request journal, how the
backup image used the API: valid token on every request, pagination followed,
nothing written by the backup, and the restore writing the seeded content back
to exactly the tampered record of the restored zone.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.request
from urllib.parse import parse_qs

from server import API, MARKER_NAME, MARKER_TYPE, MARKER_ZONE, TAMPERED_CONTENT

BASE = f"http://127.0.0.1:{os.environ.get('MOCK_PORT', '8080')}"


def call(method: str, path: str, body: dict | None = None,
         headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - localhost
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def control(method: str, path: str, body: dict | None = None) -> dict:
    status, payload = call(method, f"/__mock{path}", body)
    if status >= 300:
        raise SystemExit(f"control call {method} {path} failed: {status} {payload}")
    return payload


def marker_arg(value: str) -> str:
    # The round-trip module documents the marker as [a-z0-9-].
    if not re.fullmatch(r"[a-z0-9-]+", value):
        raise argparse.ArgumentTypeError("marker must match [a-z0-9-]+")
    return value


def marker_record(state: dict) -> dict | None:
    for rec in state["records"].get(MARKER_ZONE, []):
        if rec["type"] == MARKER_TYPE and rec["name"] == MARKER_NAME:
            return rec
    return None


def cmd_health(_args) -> int:
    try:
        return 0 if call("GET", "/__mock/health")[0] == 200 else 1
    except OSError:
        return 1


def cmd_seed(args) -> int:
    rec = control("POST", "/records", {"zone": MARKER_ZONE, "type": MARKER_TYPE,
                                       "name": MARKER_NAME, "content": args.marker})["result"]
    state = control("GET", "/state")
    position = [r["id"] for r in state["records"][MARKER_ZONE]].index(rec["id"]) + 1
    print(f"seeded {MARKER_TYPE} {MARKER_NAME} = {args.marker} "
          f"(record {position} of {len(state['records'][MARKER_ZONE])} in {MARKER_ZONE})")
    return 0


def cmd_probe_auth(_args) -> int:
    """Requests marked as probes, so `check` can tell them from the image's."""
    probe = {"X-Mock-Probe": "1"}
    cases = [
        ("no Authorization header", {}, 401),
        ("a wrong token", {"Authorization": f"Bearer {secrets.token_hex(24)}"}, 401),
        ("the configured token", {"Authorization": f"Bearer {os.environ['MOCK_API_TOKEN']}"}, 200),
    ]
    failed = 0
    for label, headers, want in cases:
        status, _ = call("GET", f"{API}/zones", headers={**probe, **headers})
        ok = status == want
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} GET /zones with {label}: HTTP {status} (expected {want})")
    return 1 if failed else 0


def cmd_tamper(args) -> int:
    rec = marker_record(control("GET", "/state"))
    if rec is None or rec["content"] != args.marker:
        print(f"FAIL the seeded record is not there to tamper with: {rec}")
        return 1
    control("POST", "/tamper", {"zone": MARKER_ZONE, "type": MARKER_TYPE, "name": MARKER_NAME,
                                "content": TAMPERED_CONTENT})
    print(f"tampered {MARKER_NAME}: {args.marker} -> {TAMPERED_CONTENT}")
    return 0


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def __call__(self, ok: bool, message: str) -> None:
        self.failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {message}")


def _page2(entries: list[dict], path: str) -> bool:
    return any(e["method"] == "GET" and e["path"] == path and e["status"] == 200
               and parse_qs(e["query"]).get("page") == ["2"] for e in entries)


def cmd_check(args) -> int:
    state = control("GET", "/state")
    entries = [e for e in control("GET", "/journal")["entries"] if not e["probe"]]
    rec = marker_record(state)
    zone_id = next(z["id"] for z in state["zones"] if z["name"] == MARKER_ZONE)
    writes = [e for e in entries if e["method"] != "GET"]
    check = Checks()

    unauthenticated = [f"{e['method']} {e['path']} ({e['auth']})"
                       for e in entries if e["auth"] != "ok"]
    check(not unauthenticated,
          f"all {len(entries)} API request(s) of the image carried the configured token"
          if not unauthenticated else
          f"{len(unauthenticated)} of {len(entries)} API request(s) lacked the configured "
          f"token: {unauthenticated[:5]}")

    if args.expect == "absent":
        check(rec is not None and rec["content"] == TAMPERED_CONTENT,
              f"{MARKER_NAME} holds the tampered content, not the marker "
              f"(content: {rec and rec['content']!r})")
        check(not writes, "the backup only read from the API (no writes so far)"
              + (f"; writes: {[(e['method'], e['path']) for e in writes]}" if writes else ""))
        return 1 if check.failed else 0

    check(rec is not None and rec["content"] == args.marker,
          f"{MARKER_NAME} holds the marker (content: {rec and rec['content']!r})")
    tampered_at = state["tampered"].get(rec["id"]) if rec else None
    if tampered_at is None:  # seeded, nothing backed up or restored yet
        check(not writes, "no API writes before the restore")
        return 1 if check.failed else 0

    # After the restore: the journal tells how the image used the API.
    record_path = f"{API}/zones/{zone_id}/dns_records/{rec['id']}"
    before = [e for e in writes if e["seq"] <= tampered_at]
    after = [e for e in writes if e["seq"] > tampered_at]
    check(not before, "the backup only read from the API")
    check(_page2(entries, f"{API}/zones"),
          "zone discovery followed the pagination (GET /zones?page=2)")
    check(_page2(entries, f"{API}/zones/{zone_id}/dns_records"),
          f"the export followed the pagination of {MARKER_ZONE}'s DNS records (page=2)")
    restored = [e for e in after if e["path"] == record_path and e["method"] in ("PUT", "PATCH")
                and e["status"] == 200 and (e.get("body") or {}).get("content") == args.marker]
    check(bool(restored), f"the restore wrote the marker back to {MARKER_NAME} "
                          f"({', '.join(e['method'] for e in restored) or 'no matching write'})")
    stray = [(e["method"], e["path"], e["status"]) for e in after if e["path"] != record_path]
    check(not stray, "the restore wrote nothing but the tampered record"
          + (f"; other writes: {stray}" if stray else ""))
    failed_writes = [(e["method"], e["path"], e["status"]) for e in after if e["status"] != 200]
    check(not failed_writes, "every restore write succeeded"
          + (f"; failed: {failed_writes}" if failed_writes else ""))
    gets = sum(e["method"] == "GET" for e in entries)
    print(f"     journal: {gets} GET, {len(writes)} write(s) by the image")
    return 1 if check.failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health").set_defaults(func=cmd_health)
    sub.add_parser("probe-auth").set_defaults(func=cmd_probe_auth)
    for name, func in (("seed", cmd_seed), ("tamper", cmd_tamper)):
        p = sub.add_parser(name)
        p.add_argument("--marker", type=marker_arg, required=True)
        p.set_defaults(func=func)
    p = sub.add_parser("check")
    p.add_argument("--marker", type=marker_arg, required=True)
    p.add_argument("--expect", choices=("present", "absent"), required=True)
    p.set_defaults(func=cmd_check)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
