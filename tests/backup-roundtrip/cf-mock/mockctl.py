#!/usr/bin/env python3
"""Control and assertions for the mock Cloudflare API (runs inside cf-mock).

    mockctl.py health                       compose healthcheck
    mockctl.py seed --marker M              add the marker data (see server._seed)
    mockctl.py probe-auth                   prove the API rejects bad credentials
    mockctl.py tamper --marker M            change the marker data out of band
    mockctl.py check --marker M --expect present|absent
    mockctl.py account-id                   print the account id
    mockctl.py snapshot < export.json       check a snapshot's export (manifest.py)

The test scripts run it with `docker compose exec -T cf-mock python
/mock/mockctl.py ...`; the marker is passed as an argument, never pasted into
code. `check` asserts the marker data and, from the request journal, how the
backup image used the API: the token on every request, nothing but modeled
routes, every curated resource type asked for, pagination followed, rulesets
listed by cf-terraforming's legacy client through the mock, nothing written by
the backup, and the restore changing exactly the tampered resources back.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import ssl
import sys
import urllib.error
import urllib.request
from collections import Counter
from urllib.parse import parse_qs

from server import (
    ACCOUNT_TYPES_WITH_DATA,
    API,
    MARKER_NAME,
    MARKER_RULE_EXPRESSION,
    MARKER_RULE_REF,
    MARKER_SETTING,
    MARKER_TYPE,
    MARKER_ZONE,
    MARKER_ZONE_TYPES_WITH_DATA,
    SEEDED_SETTING,
    TAMPERED_CONTENT,
    TAMPERED_SETTING,
    ZONE_TYPES_WITH_DATA,
    ZONES,
)

CONTROL_BASE = f"http://127.0.0.1:{os.environ.get('MOCK_CONTROL_PORT', '8080')}"
API_BASE = (f"https://{os.environ.get('MOCK_HOST', 'cf-mock')}:"
            f"{os.environ.get('MOCK_PORT', '8443')}")
CA_FILE = os.path.join(os.environ.get("MOCK_TLS_DIR", "/tls"), "ca.pem")
LEGACY = "cf-terraforming legacy (cloudflare-go v0)"


def call(url: str, method: str = "GET", body: dict | None = None,
         headers: dict | None = None, context: ssl.SSLContext | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10, context=context) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def control(method: str, path: str, body: dict | None = None) -> dict:
    status, payload = call(f"{CONTROL_BASE}/__mock{path}", method, body)
    if status >= 300:
        raise SystemExit(f"control call {method} {path} failed: {status} {payload}")
    return payload


def marker_arg(value: str) -> str:
    # The round-trip module documents the marker as [a-z0-9-].
    if not re.fullmatch(r"[a-z0-9-]+", value):
        raise argparse.ArgumentTypeError("marker must match [a-z0-9-]+")
    return value


def zone_id(state: dict, name: str) -> str:
    return next(z["id"] for z in state["zones"] if z["name"] == name)


def marker_record(state: dict) -> dict | None:
    for rec in state["records"].get(MARKER_ZONE, []):
        if rec["type"] == MARKER_TYPE and rec["name"] == MARKER_NAME:
            return rec
    return None


def custom_ruleset(state: dict) -> dict:
    return next(rs for rs in state["rulesets"][f"zones/{MARKER_ZONE}"] if rs["kind"] == "zone")


def marker_rule(state: dict) -> dict | None:
    return next((r for r in custom_ruleset(state)["rules"] if r["ref"] == MARKER_RULE_REF), None)


def marker_setting(state: dict):
    return state["settings"][MARKER_ZONE][MARKER_SETTING]["value"]


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def __call__(self, ok: bool, message: str) -> None:
        self.failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {message}")

    @property
    def status(self) -> int:
        return 1 if self.failed else 0


def cmd_health(_args) -> int:
    try:
        return 0 if call(f"{CONTROL_BASE}/__mock/health")[0] == 200 else 1
    except OSError:
        return 1


def cmd_account_id(_args) -> int:
    print(control("GET", "/state")["account"]["id"])
    return 0


def cmd_seed(args) -> int:
    seeded = control("POST", "/seed", {"marker": args.marker})
    state = control("GET", "/state")
    ids = [r["id"] for r in state["records"][MARKER_ZONE]]
    position = ids.index(seeded["record"]["id"]) + 1
    print(f"seeded {MARKER_TYPE} {MARKER_NAME} = {args.marker} "
          f"(record {position} of {len(ids)} in {MARKER_ZONE})")
    print(f"seeded rule {MARKER_RULE_REF!r} in the custom firewall ruleset of {MARKER_ZONE}, "
          f"described by the marker")
    print(f"seeded zone setting {MARKER_SETTING} = {SEEDED_SETTING} in {MARKER_ZONE}")
    return 0


def cmd_probe_auth(_args) -> int:
    """Requests marked as probes, so `check` can tell them from the image's.
    They use the TLS endpoint and its CA, like the image does."""
    context = ssl.create_default_context(cafile=CA_FILE)
    probe = {"X-Mock-Probe": "1"}
    cases = [
        ("no Authorization header", {}, 401),
        ("a wrong token", {"Authorization": f"Bearer {secrets.token_hex(24)}"}, 401),
        ("the configured token", {"Authorization": f"Bearer {os.environ['MOCK_API_TOKEN']}"}, 200),
    ]
    check = Checks()
    for label, headers, want in cases:
        status, _ = call(f"{API_BASE}{API}/zones", headers={**probe, **headers},
                         context=context)
        check(status == want, f"GET /zones over TLS with {label}: HTTP {status} (expected {want})")
    return check.status


def cmd_tamper(args) -> int:
    state = control("GET", "/state")
    rec, rule = marker_record(state), marker_rule(state)
    if rec is None or rec["content"] != args.marker or rule is None \
            or rule["description"] != args.marker:
        print(f"FAIL the seeded data is not there to tamper with: {rec}, {rule}")
        return 1
    control("POST", "/tamper", {"marker": args.marker})
    print(f"tampered {MARKER_NAME}: {args.marker} -> {TAMPERED_CONTENT}")
    print(f"tampered rule {MARKER_RULE_REF!r}: description -> {TAMPERED_CONTENT}, disabled")
    print(f"tampered zone setting {MARKER_SETTING}: {SEEDED_SETTING} -> {TAMPERED_SETTING}")
    return 0


def _page2(entries: list[dict], route: str, path: str | None = None) -> bool:
    return any(e["method"] == "GET" and e["route"] == route and e["status"] == 200
               and (path is None or e["path"] == path)
               and parse_qs(e["query"]).get("page") == ["2"] for e in entries)


def _restored(entry: dict, kind: str, marker: str) -> bool:
    """Whether a restore write puts the seeded value back."""
    body = entry.get("body") or {}
    if kind == "dns_record":
        return body.get("content") == marker
    if kind == "ruleset":
        return any(r.get("ref") == MARKER_RULE_REF and r.get("description") == marker
                   and r.get("enabled", True) is True
                   and r.get("expression") == MARKER_RULE_EXPRESSION
                   for r in body.get("rules") or [])
    return body.get("value") == SEEDED_SETTING


def cmd_check(args) -> int:
    state = control("GET", "/state")
    entries = [e for e in control("GET", "/journal")["entries"] if not e["probe"]]
    rec, rule, setting = marker_record(state), marker_rule(state), marker_setting(state)
    writes = [e for e in entries if e["method"] != "GET" and not e["dry_run"]]
    check = Checks()

    unauthenticated = [f"{e['method']} {e['path']} ({e['auth']})"
                       for e in entries if e["auth"] != "ok"]
    check(not unauthenticated,
          f"all {len(entries)} API request(s) of the image carried the configured token"
          if not unauthenticated else
          f"{len(unauthenticated)} of {len(entries)} API request(s) lacked the configured "
          f"token: {unauthenticated[:5]}")
    unknown = sorted({f"{e['method']} {e['path']}" for e in entries if e["route"] is None})
    check(not unknown, "every request of the image hit a modeled route"
          + (f"; unmodeled: {unknown[:10]}" if unknown else ""))

    if args.expect == "absent":
        check(rec is not None and rec["content"] == TAMPERED_CONTENT,
              f"{MARKER_NAME} holds the tampered content (content: {rec and rec['content']!r})")
        check(rule is not None and rule["description"] == TAMPERED_CONTENT
              and rule["enabled"] is False,
              f"rule {MARKER_RULE_REF!r} is tampered "
              f"(description {rule and rule['description']!r}, enabled {rule and rule['enabled']})")
        check(setting == TAMPERED_SETTING, f"{MARKER_SETTING} is tampered (value {setting!r})")
        check(not writes, "the backup only read from the API (no writes so far)"
              + (f"; writes: {[(e['method'], e['path']) for e in writes]}" if writes else ""))
        return check.status

    check(rec is not None and rec["content"] == args.marker,
          f"{MARKER_NAME} holds the marker (content: {rec and rec['content']!r})")
    check(rule is not None and rule["description"] == args.marker and rule["enabled"] is True
          and rule["expression"] == MARKER_RULE_EXPRESSION
          and rule["action"] == "managed_challenge",
          f"rule {MARKER_RULE_REF!r} of {MARKER_ZONE}'s custom firewall ruleset holds the "
          f"marker (description {rule and rule['description']!r}, "
          f"enabled {rule and rule['enabled']})")
    check(setting == SEEDED_SETTING,
          f"{MARKER_SETTING} of {MARKER_ZONE} holds the seeded value (value {setting!r})")
    if not state["tampered"]:  # seeded, nothing backed up or restored yet
        check(not writes, "no API writes before the restore")
        return check.status

    tampered_at = min(state["tampered"].values())
    backup = [e for e in entries if e["seq"] <= tampered_at]
    before = [e for e in writes if e["seq"] <= tampered_at]
    after = [e for e in writes if e["seq"] > tampered_at]
    check(not before, "the backup only read from the API")

    # The export asked for every curated type at its scope.
    routes = {r["name"]: r["resource_type"] for r in state["routes"] if r["resource_type"]}
    asked = {e["route"] for e in backup if e["method"] == "GET" and e["status"] == 200}
    missing = sorted(f"{rtype} ({name})" for name, rtype in routes.items() if name not in asked)
    check(not missing, f"the backup asked for all {len(set(routes.values()))} curated resource "
                       f"types the API serves"
          + (f"; never asked: {missing}" if missing else ""))
    print(f"     not asked by design (cf-terraforming 0.27 cannot list them): "
          f"{', '.join(state['not_requested'])}")

    charlie = zone_id(state, MARKER_ZONE)
    check(_page2(backup, "zones"), "zone discovery followed the pagination (GET /zones?page=2)")
    check(_page2(backup, "dns_records", f"{API}/zones/{charlie}/dns_records"),
          f"the export followed the pagination of {MARKER_ZONE}'s DNS records (page=2)")
    check(_page2(backup, "kv_namespaces"),
          "the export followed the pagination of the Workers KV namespaces (page=2)")

    # cf-terraforming's legacy client lists rulesets: it must have reached the mock.
    account = state["account"]["id"]
    containers = {f"{API}/zones/{z['id']}/rulesets": z["name"] for z in state["zones"]}
    containers[f"{API}/accounts/{account}/rulesets"] = "the account"
    listed = {e["path"] for e in backup if e["client"] == LEGACY and e["route"] == "rulesets"}
    unlisted = sorted(name for path, name in containers.items() if path not in listed)
    check(not unlisted, "cf-terraforming's legacy client listed the rulesets of all 3 zones and "
                        "the account through the mock"
          + (f"; not listed for: {unlisted}" if unlisted else ""))
    custom = [rs for key, rulesets in state["rulesets"].items() for rs in rulesets
              if rs["kind"] != "managed"]
    fetched = {e["path"].rsplit("/", 1)[1] for e in backup
               if e["client"] == LEGACY and e["route"] == "ruleset"}
    check(all(rs["id"] in fetched for rs in custom),
          f"it fetched the rules of all {len(custom)} custom rulesets "
          f"({len(fetched)} ruleset GET(s), managed rulesets left out)")

    # The restore changed exactly the tampered resources back.
    targets = {}
    for key in state["tampered"]:
        kind, ident = key.split("/", 1)
        path = {"dns_record": f"{API}/zones/{charlie}/dns_records/{ident}",
                "ruleset": f"{API}/zones/{charlie}/rulesets/{ident}",
                "zone_setting": f"{API}/zones/{charlie}/settings/{ident}"}[kind]
        targets[path] = kind
    for path, kind in sorted(targets.items(), key=lambda t: t[1]):
        hits = [e for e in after if e["path"] == path and e["status"] < 300 and e["changed"]
                and _restored(e, kind, args.marker)]
        check(bool(hits), f"the restore wrote the seeded {kind.replace('_', ' ')} back "
                          f"({', '.join(e['method'] for e in hits) or 'no matching write'})")
    stray = [(e["method"], e["path"]) for e in after if e["changed"] and e["path"] not in targets]
    check(not stray, "the restore changed nothing but the tampered resources"
          + (f"; other changes: {stray}" if stray else ""))
    failed = [(e["method"], e["path"], e["status"]) for e in after if e["status"] >= 300]
    check(not failed, "every restore write succeeded"
          + (f"; failed: {failed}" if failed else ""))
    noop = [(e["method"], e["path"]) for e in after if not e["changed"] and e["status"] < 300]
    if noop:
        print(f"     restore writes that changed nothing: {noop}")
    dry_runs = sum(1 for e in entries if e["dry_run"])
    clients = Counter(e["client"] for e in entries)
    print(f"     journal: {sum(e['method'] == 'GET' for e in entries)} GET, {len(writes)} "
          f"write(s), {dry_runs} dry run(s) by the image; clients: {dict(clients)}")
    return check.status


def cmd_snapshot(_args) -> int:
    """Checks the export of a snapshot, as printed by manifest.py on stdin."""
    export = json.load(sys.stdin)
    state = control("GET", "/state")
    account = state["account"]["id"]
    check = Checks()
    check(export.get("zone_count") == len(ZONES) and sorted(export.get("zones", [])) == list(ZONES),
          f"the snapshot covers the {len(ZONES)} zones (zones: {export.get('zones')})")
    check(export.get("account_ids") == [account], "the snapshot covers the account")
    errors = export.get("errors") or []
    check(not errors, "the export recorded no errors" + (f": {errors}" if errors else ""))

    expected = {"main.tf", f"_account/{account}/imports.tf"}
    expected |= {f"_account/{account}/{t}.tf" for t in ACCOUNT_TYPES_WITH_DATA}
    for zone in ZONES:
        types = ZONE_TYPES_WITH_DATA + (MARKER_ZONE_TYPES_WITH_DATA if zone == MARKER_ZONE else ())
        expected |= {f"zones/{zone}/{t}.tf" for t in types} | {f"zones/{zone}/imports.tf"}
    files = set(export.get("files", []))
    missing, unexpected = sorted(expected - files), sorted(files - expected)
    check(not missing and not unexpected,
          f"the snapshot holds the {len(expected)} expected files"
          + (f"; missing: {missing}" if missing else "")
          + (f"; unexpected: {unexpected}" if unexpected else ""))
    for line in export.get("skipped", []):
        print(f"     skipped: {line}")
    return check.status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("health", cmd_health), ("probe-auth", cmd_probe_auth),
                       ("account-id", cmd_account_id), ("snapshot", cmd_snapshot)):
        sub.add_parser(name).set_defaults(func=func)
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
