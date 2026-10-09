#!/usr/bin/env python3
"""Mock Cloudflare API v4 for the CI backup round trip (stdlib only).

Serves exactly the endpoints the cloudflare-backup image calls when it backs
up and restores the DNS records of a zone:

  GET   /client/v4/zones                             zone discovery (plugin)
  GET   /client/v4/zones/{zone_id}/dns_records       export (cf-terraforming)
  GET   /client/v4/zones/{zone_id}/dns_records/{id}  import + refresh (OpenTofu)
  PUT   /client/v4/zones/{zone_id}/dns_records/{id}  restore (OpenTofu provider)
  PATCH /client/v4/zones/{zone_id}/dns_records/{id}  restore, edit variant

Like the real API it answers with the {success, errors, messages, result}
envelope, needs "Authorization: Bearer <MOCK_API_TOKEN>" on every request and
paginates lists (page/per_page, result_info.total_pages). Pages hold at most
MOCK_PAGE_SIZE items, so a client that does not follow total_pages misses data.
Anything else under /client/v4 is a 404 "No route for that URI".

Every API request lands in a journal (method, path, query, auth result, status,
request body of writes; never the Authorization header). /__mock/* is the
unauthenticated control plane of the test scripts (see mockctl.py): it only
exists on the compose network of the CI round trip and is never published.

Ids are derived from names at runtime (sha256), so nothing id- or token-like is
stored in the repository.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import sys
import threading
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

API = "/client/v4"
CONTROL = "/__mock"

# Fixture: three zones, so the zone list spans two pages at MOCK_PAGE_SIZE=2,
# and two records in the marker zone, so the record the seed adds lands on the
# second page of that zone's DNS records as well. The restore can only bring
# the marker back if both the zone discovery and cf-terraforming paginated.
ACCOUNT_NAME = "Backup Round Trip"
ZONES = ("alpha.example", "bravo.example", "charlie.example")
FIXTURE_RECORDS = (
    ("alpha.example", "A", "alpha.example", "192.0.2.1"),
    ("bravo.example", "A", "bravo.example", "192.0.2.2"),
    ("charlie.example", "A", "charlie.example", "192.0.2.3"),
    ("charlie.example", "A", "www.charlie.example", "192.0.2.4"),
)
MARKER_ZONE = "charlie.example"
MARKER_TYPE = "TXT"
MARKER_NAME = "_roundtrip.charlie.example"
TAMPERED_CONTENT = "tampered-by-roundtrip"

# Fields a client may set on a DNS record (PUT/PATCH); the rest is computed.
WRITABLE = ("name", "type", "content", "ttl", "proxied", "comment", "tags",
            "settings", "priority", "data", "private_routing")
PROXIABLE_TYPES = {"A", "AAAA", "CNAME"}

_ROUTE_RECORDS = re.compile(rf"^{API}/zones/([0-9a-f]{{32}})/dns_records$")
_ROUTE_RECORD = re.compile(rf"^{API}/zones/([0-9a-f]{{32}})/dns_records/([0-9a-f]{{32}})$")


def ident(*parts: str) -> str:
    """A 32-hex id like Cloudflare's, stable for the same name."""
    return hashlib.sha256("/".join(parts).encode()).hexdigest()[:32]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def fqdn(name: str, zone: str) -> str:
    """The API stores record names fully qualified, like Cloudflare does."""
    name = name.strip().rstrip(".")
    if name in ("", "@"):
        return zone
    return name if name == zone or name.endswith("." + zone) else f"{name}.{zone}"


class ApiError(Exception):
    def __init__(self, status: int, code: int, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Store:
    """Zones, records and the request journal, guarded by one lock."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        created = now()
        self.account = {"id": ident("account", ACCOUNT_NAME), "name": ACCOUNT_NAME}
        self.zones = [{
            "id": ident("zone", name), "name": name, "status": "active", "paused": False,
            "type": "full", "account": dict(self.account),
            "created_on": created, "modified_on": created,
        } for name in ZONES]
        self.records: dict[str, list[dict]] = {z["id"]: [] for z in self.zones}
        self.journal: list[dict] = []
        self.tampered: dict[str, int] = {}  # record id -> journal seq at the tamper
        for zone, rtype, name, content in FIXTURE_RECORDS:
            self.add_record(zone, rtype, name, content)

    def zone_by_name(self, name: str) -> dict:
        for zone in self.zones:
            if zone["name"] == name:
                return zone
        raise ApiError(404, 1001, f"unknown zone {name!r}")

    def zone_by_id(self, zone_id: str, path: str) -> dict:
        for zone in self.zones:
            if zone["id"] == zone_id:
                return zone
        raise ApiError(404, 7003, f"Could not route to {path}, perhaps your object "
                                  "identifier is invalid?")

    def record(self, zone_id: str, record_id: str) -> dict:
        for rec in self.records[zone_id]:
            if rec["id"] == record_id:
                return rec
        raise ApiError(404, 81044, "Record does not exist.")

    def add_record(self, zone_name: str, rtype: str, name: str, content: str,
                   ttl: int = 300) -> dict:
        zone = self.zone_by_name(zone_name)
        name = fqdn(name, zone_name)
        stamp = now()
        rec = {
            "id": ident("record", zone_name, rtype, name), "name": name, "type": rtype,
            "content": content, "proxiable": rtype in PROXIABLE_TYPES, "proxied": False,
            "ttl": ttl, "settings": {}, "meta": {}, "comment": None, "tags": [],
            "created_on": stamp, "modified_on": stamp,
        }
        records = self.records[zone["id"]]
        records[:] = [r for r in records if r["id"] != rec["id"]] + [rec]
        return rec

    def find(self, zone_name: str, rtype: str, name: str) -> dict:
        zone = self.zone_by_name(zone_name)
        for rec in self.records[zone["id"]]:
            if rec["type"] == rtype and rec["name"] == fqdn(name, zone_name):
                return rec
        raise ApiError(404, 81044, f"no {rtype} record {name!r} in {zone_name}")


# Set up by main(); module level so mockctl.py can import the fixture names.
STORE: Store
TOKEN = ""
PAGE_SIZE = 2


def _int_param(query: dict, key: str, default: int) -> int:
    raw = query.get(key, [str(default)])[0]
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(400, 1001, f"{key} must be an integer, got {raw!r}") from None
    if value < 1:
        raise ApiError(400, 1001, f"{key} must be >= 1")
    return value


def paginate(items: list, query: dict) -> tuple[list, dict]:
    """Slice ``items`` like the API: per_page is capped at PAGE_SIZE."""
    page = _int_param(query, "page", 1)
    per_page = min(_int_param(query, "per_page", PAGE_SIZE), PAGE_SIZE)
    start = (page - 1) * per_page
    chunk = items[start:start + per_page]
    info = {"page": page, "per_page": per_page, "count": len(chunk),
            "total_count": len(items), "total_pages": math.ceil(len(items) / per_page)}
    return chunk, info


class Handler(BaseHTTPRequestHandler):
    server_version = "cf-mock/1"
    protocol_version = "HTTP/1.1"

    # -- plumbing -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._dispatch("GET")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def log_message(self, fmt: str, *args) -> None:
        # One line per request on stdout (the compose log); no headers.
        sys.stdout.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _auth(self) -> str:
        header = self.headers.get("Authorization")
        if not header:
            return "missing"
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(value.strip().encode(),
                                                                 TOKEN.encode()):
            return "invalid"
        return "ok"

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        raw = self._body()  # always drained: the connection is kept alive
        if url.path == CONTROL or url.path.startswith(CONTROL + "/"):
            self._control(method, url.path, raw)
            return
        self._api(method, url.path, url.query, raw)

    # -- Cloudflare API ---------------------------------------------------------
    def _api(self, method: str, path: str, query_string: str, raw: bytes) -> None:
        query = parse_qs(query_string)
        entry = {"method": method, "path": path, "query": query_string,
                 "probe": self.headers.get("X-Mock-Probe") == "1"}
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        if method in ("PUT", "PATCH", "POST"):
            entry["body"] = body
        auth = self._auth()
        entry["auth"] = auth
        try:
            if auth != "ok":
                raise ApiError(401, 10000, "Authentication error")
            if not path.startswith(API + "/"):
                raise ApiError(404, 7000, "No route for that URI")
            status, payload = self._route(method, path, query, raw, body)
        except ApiError as exc:
            status, code, message = exc.status, exc.code, exc.message
        except Exception as exc:  # noqa: BLE001 - a mock bug must show up as a 500
            traceback.print_exc()
            status, code, message = 500, 10001, f"mock error: {exc}"
        if status >= 400:
            payload = {"success": False, "errors": [{"code": code, "message": message}],
                       "messages": [], "result": None}
        with STORE.lock:
            entry["seq"] = len(STORE.journal) + 1
            entry["status"] = status
            STORE.journal.append(entry)
        self._send(status, payload)

    def _route(self, method: str, path: str, query: dict, raw: bytes, body) -> tuple[int, dict]:
        if path == f"{API}/zones":
            self._allow(method, "GET")
            with STORE.lock:
                zones = list(STORE.zones)
            if "account.id" in query:
                zones = [z for z in zones if z["account"]["id"] == query["account.id"][0]]
            if "name" in query:
                zones = [z for z in zones if z["name"] == query["name"][0]]
            return self._list(zones, query)

        match = _ROUTE_RECORDS.match(path)
        if match:
            self._allow(method, "GET")
            with STORE.lock:
                STORE.zone_by_id(match.group(1), path)
                records = [dict(r) for r in STORE.records[match.group(1)]]
            return self._list(records, query)

        match = _ROUTE_RECORD.match(path)
        if match:
            self._allow(method, "GET", "PUT", "PATCH")
            zone_id, record_id = match.groups()
            with STORE.lock:
                zone = STORE.zone_by_id(zone_id, path)
                rec = STORE.record(zone_id, record_id)
                if method != "GET":
                    self._write(rec, zone["name"], method, raw, body)
                return 200, self._ok(dict(rec))

        raise ApiError(404, 7000, "No route for that URI")

    @staticmethod
    def _allow(method: str, *allowed: str) -> None:
        if method not in allowed:
            raise ApiError(405, 10405, f"Method {method} not allowed for this route")

    @staticmethod
    def _ok(result) -> dict:
        return {"success": True, "errors": [], "messages": [], "result": result}

    def _list(self, items: list, query: dict) -> tuple[int, dict]:
        chunk, info = paginate(items, query)
        payload = self._ok(chunk)
        payload["result_info"] = info
        return 200, payload

    @staticmethod
    def _write(rec: dict, zone_name: str, method: str, raw: bytes, body) -> None:
        """PUT replaces the writable fields it names, PATCH merges them."""
        if not isinstance(body, dict):
            raise ApiError(400, 9207, "Request body is invalid JSON" if raw else
                           "Request body is missing")
        if method == "PUT":
            for key in ("name", "type"):
                if not isinstance(body.get(key), str) or not body[key]:
                    raise ApiError(400, 9000, f"DNS record {key} is required")
        if "ttl" in body and not isinstance(body["ttl"], (int, float)):
            raise ApiError(400, 9000, "DNS record ttl must be a number")
        for key in WRITABLE:
            if key in body:
                rec[key] = body[key]
        rec["name"] = fqdn(rec["name"], zone_name)
        rec["proxiable"] = rec["type"] in PROXIABLE_TYPES
        rec["modified_on"] = now()

    # -- control plane (test scripts only) --------------------------------------
    def _control(self, method: str, path: str, raw: bytes) -> None:
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            self._send(400, {"error": "body is not JSON"})
            return
        try:
            with STORE.lock:
                status, payload = self._control_route(method, path, body)
        except ApiError as exc:
            status, payload = exc.status, {"error": exc.message}
        except (KeyError, TypeError) as exc:
            status, payload = 400, {"error": f"bad request: {exc}"}
        self._send(status, payload)

    @staticmethod
    def _control_route(method: str, path: str, body: dict) -> tuple[int, dict]:
        if (method, path) == ("GET", f"{CONTROL}/health"):
            return 200, {"ok": True}
        if (method, path) == ("GET", f"{CONTROL}/state"):
            names = {z["id"]: z["name"] for z in STORE.zones}
            return 200, {"zones": STORE.zones,
                         "records": {names[zid]: recs for zid, recs in STORE.records.items()},
                         "tampered": STORE.tampered}
        if (method, path) == ("GET", f"{CONTROL}/journal"):
            return 200, {"entries": STORE.journal}
        if (method, path) == ("POST", f"{CONTROL}/records"):
            rec = STORE.add_record(body["zone"], body["type"], body["name"], body["content"],
                                   int(body.get("ttl", 300)))
            return 201, {"result": rec}
        if (method, path) == ("POST", f"{CONTROL}/tamper"):
            # An out-of-band change (someone edits the record in the dashboard).
            rec = STORE.find(body["zone"], body["type"], body["name"])
            rec["content"] = body["content"]
            rec["modified_on"] = now()
            STORE.tampered[rec["id"]] = len(STORE.journal)
            return 200, {"result": rec}
        raise ApiError(404, 0, f"no control route {method} {path}")


def main() -> int:
    global STORE, TOKEN, PAGE_SIZE
    TOKEN = os.environ.get("MOCK_API_TOKEN", "")
    if len(TOKEN) < 16:
        print("MOCK_API_TOKEN must be set (16+ characters)", file=sys.stderr)
        return 2
    PAGE_SIZE = int(os.environ.get("MOCK_PAGE_SIZE", "2"))
    port = int(os.environ.get("MOCK_PORT", "8080"))
    STORE = Store()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 - compose network only
    server.daemon_threads = True
    print(f"cf-mock listening on :{port}{API} (page size {PAGE_SIZE}, "
          f"{len(STORE.zones)} zones)", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
