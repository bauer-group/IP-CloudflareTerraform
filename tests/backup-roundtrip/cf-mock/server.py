#!/usr/bin/env python3
"""Mock Cloudflare API v4 for the CI backup round trip (stdlib only).

Serves, over TLS, the endpoints the cloudflare-backup image calls when it backs
up an account with the plugin's curated default resource types and restores a
zone of it. Every API client in the image lands here: the plugin's zone and
tunnel discovery (Python), cf-terraforming - its cloudflare-go v4 client and,
for cloudflare_ruleset, its legacy cloudflare-go v0 client, which only speaks
https - and the OpenTofu provider.

Fixture (see Store): the account "Backup Round Trip" with two custom rulesets
(a root entry point that executes a custom ruleset) and a managed one, three
Workers KV namespaces, a Cloudflare Tunnel and its ingress configuration, a
load balancer monitor and pool and an R2 bucket; three zones (alpha, bravo,
charlie.example), each with DNS records, the 27 zone settings the plugin
exports, bot management, URL normalization and managed transforms, each
listing a managed ruleset; charlie.example also has a custom firewall ruleset
and a load balancer. Every other curated type answers with an empty list. Ids
are derived from names at runtime (sha256), so nothing id- or token-like is
stored in the repository.

Like the real API it answers with the {success, errors, messages, result}
envelope, needs "Authorization: Bearer <MOCK_API_TOKEN>" on every request and
paginates the lists the real API paginates (page/per_page,
result_info.total_pages) in pages of MOCK_PAGE_SIZE items, so a client that
does not follow total_pages misses data. Rulesets are listed in one page, like
the real API does. A PUT with ?dry_run=true - the provider validates ruleset
changes that way while planning - answers without storing anything. Anything
else under /client/v4 is a 404 "No route for that URI".

Every API request lands in a journal: method, path, query, client (from the
User-Agent), auth result, status, the route that answered, and for writes the
request body and whether it changed anything; never the Authorization header.
The control plane of the test scripts (/__mock/*, see mockctl.py) listens on
plain HTTP on 127.0.0.1 only - reachable from inside this container, never from
the image under test.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import os
import re
import ssl
import sys
import threading
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

API = "/client/v4"
CONTROL = "/__mock"

# -- Fixture --------------------------------------------------------------------
# Three zones, so the zone list spans two pages at MOCK_PAGE_SIZE=2, and two
# records in the marker zone, so the record the seed adds lands on the second
# page of that zone's DNS records as well.
ACCOUNT_NAME = "Backup Round Trip"
ZONES = ("alpha.example", "bravo.example", "charlie.example")
FIXTURE_RECORDS = (
    ("alpha.example", "A", "alpha.example", "192.0.2.1"),
    ("bravo.example", "A", "bravo.example", "192.0.2.2"),
    ("charlie.example", "A", "charlie.example", "192.0.2.3"),
    ("charlie.example", "A", "www.charlie.example", "192.0.2.4"),
)
# What the seed adds, the tamper changes and the restore has to bring back -
# all in charlie.example: a TXT record holding the run's marker, a rule of the
# zone's custom firewall ruleset described by the marker, and a zone setting.
MARKER_ZONE = "charlie.example"
MARKER_TYPE = "TXT"
MARKER_NAME = "_roundtrip.charlie.example"
TAMPERED_CONTENT = "tampered-by-roundtrip"
MARKER_RULE_REF = "roundtrip_marker"
MARKER_RULE_EXPRESSION = '(http.request.uri.path eq "/roundtrip")'
MARKER_SETTING = "min_tls_version"
SEEDED_SETTING = "1.2"
TAMPERED_SETTING = "1.0"
TUNNEL_NAME = "roundtrip-tunnel"
KV_NAMESPACES = ("sessions", "feature-flags", "rate-limits")  # two pages

# The zone settings the plugin exports by default (resources.ZONE_SETTING_IDS),
# with the values of a fresh zone.
ZONE_SETTINGS: dict[str, object] = {
    "always_online": "off", "always_use_https": "off", "automatic_https_rewrites": "on",
    "brotli": "on", "browser_cache_ttl": 14400, "browser_check": "on",
    "cache_level": "aggressive", "challenge_ttl": 1800, "development_mode": "off",
    "early_hints": "off", "email_obfuscation": "on", "hotlink_protection": "off",
    "http3": "on", "ip_geolocation": "on", "ipv6": "on", "min_tls_version": "1.0",
    "opportunistic_encryption": "on", "origin_error_page_pass_thru": "off",
    "rocket_loader": "off",
    "security_header": {"strict_transport_security": {
        "enabled": False, "max_age": 0, "include_subdomains": False, "preload": False,
        "nosniff": False}},
    "security_level": "medium", "server_side_exclude": "on",
    "sort_query_string_for_cache": "off", "ssl": "full", "tls_1_3": "on",
    "websockets": "on", "0rtt": "off",
}
BOT_MANAGEMENT = {"enable_js": False, "fight_mode": False, "ai_bots_protection": "disabled",
                  "crawler_protection": "disabled", "is_robots_txt_managed": False,
                  "using_latest_model": True}
URL_NORMALIZATION = {"type": "cloudflare", "scope": "incoming"}
MANAGED_HEADERS = {
    "managed_request_headers": [
        {"id": "add_bot_protection_headers", "enabled": False, "has_conflict": False},
        {"id": "add_visitor_location_headers", "enabled": True, "has_conflict": False},
    ],
    "managed_response_headers": [
        {"id": "add_security_headers", "enabled": True, "has_conflict": False},
        {"id": "remove_x-powered-by_header", "enabled": False, "has_conflict": False},
    ],
}

# What the export has to write: one file per scope and type with resources.
# The empty snippet rules cf-terraforming 0.27 wraps into a resource for every
# zone are dropped by the plugin (cfterraforming.adapt_to_provider).
ZONE_TYPES_WITH_DATA = (
    "cloudflare_bot_management", "cloudflare_dns_record", "cloudflare_managed_transforms",
    "cloudflare_url_normalization_settings", "cloudflare_zone_setting",
)
MARKER_ZONE_TYPES_WITH_DATA = ("cloudflare_load_balancer", "cloudflare_ruleset")
ACCOUNT_TYPES_WITH_DATA = (
    "cloudflare_load_balancer_monitor", "cloudflare_load_balancer_pool",
    "cloudflare_r2_bucket", "cloudflare_ruleset", "cloudflare_workers_kv_namespace",
    "cloudflare_zero_trust_tunnel_cloudflared",
    "cloudflare_zero_trust_tunnel_cloudflared_config",
)
# Fields the API computes; a write never sets them.
COMPUTED_FIELDS = {"id", "created_on", "modified_on", "zone_name", "networks"}

# Fields a client may set on a DNS record (PUT/PATCH); the rest is computed.
RECORD_FIELDS = ("name", "type", "content", "ttl", "proxied", "comment", "tags",
                 "settings", "priority", "data", "private_routing")
PROXIABLE_TYPES = {"A", "AAAA", "CNAME"}
RULE_ACTIONS = {"block", "challenge", "compress_response", "execute", "js_challenge", "log",
                "managed_challenge", "redirect", "rewrite", "route", "score", "serve_error",
                "set_cache_settings", "set_config", "skip"}
RULE_OPTIONAL = ("action_parameters", "logging", "ratelimit", "exposed_credential_check")
# Left out when telling whether a write changed something.
VOLATILE = {"modified_on", "last_updated", "version", "created_on", "time_remaining"}


def ident(*parts: str) -> str:
    """A 32-hex id like Cloudflare's, stable for the same name."""
    return hashlib.sha256("/".join(parts).encode()).hexdigest()[:32]


def uuid_ident(*parts: str) -> str:
    """A UUID-shaped id (Cloudflare Tunnel ids), stable for the same name."""
    h = ident(*parts)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def fqdn(name: str, zone: str) -> str:
    """The API stores record names fully qualified, like Cloudflare does."""
    name = name.strip().rstrip(".")
    if name in ("", "@"):
        return zone
    return name if name == zone or name.endswith("." + zone) else f"{name}.{zone}"


def semantic(value):
    """``value`` without the fields the API stamps on every write."""
    if isinstance(value, dict):
        return {k: semantic(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [semantic(v) for v in value]
    return value


def ruleset_semantic(ruleset: dict) -> dict:
    """What a ruleset write may change: its description and rules (rule ids
    excluded - a rewrite of the same rules keeps their meaning)."""
    rules = [{k: v for k, v in semantic(rule).items() if k != "id"}
             for rule in ruleset.get("rules", [])]
    return {"description": ruleset.get("description", ""), "rules": rules}


class ApiError(Exception):
    def __init__(self, status: int, code: int, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def not_routable(path: str) -> ApiError:
    return ApiError(404, 7003, f"Could not route to {path}, perhaps your object "
                               "identifier is invalid?")


class Store:
    """The account, its zones and resources, and the request journal - all
    guarded by one lock."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        created = now()
        self.account = {"id": ident("account", ACCOUNT_NAME), "name": ACCOUNT_NAME}
        acct = self.account["id"]
        self.zones = [{
            "id": ident("zone", name), "name": name, "status": "active", "paused": False,
            "type": "full", "account": dict(self.account),
            "created_on": created, "modified_on": created,
        } for name in ZONES]
        self.records: dict[str, list[dict]] = {z["id"]: [] for z in self.zones}
        self.settings: dict[str, dict[str, dict]] = {}
        self.singletons: dict[tuple[str, str], dict] = {}
        # Rulesets by container: ("zones", zone_id) or ("accounts", account_id).
        self.rulesets: dict[tuple[str, str], list[dict]] = {}
        for zone in self.zones:
            zid = zone["id"]
            self.settings[zid] = {
                sid: self._setting(sid, value) for sid, value in ZONE_SETTINGS.items()}
            self.singletons[(zid, "bot_management")] = copy.deepcopy(BOT_MANAGEMENT)
            self.singletons[(zid, "url_normalization")] = copy.deepcopy(URL_NORMALIZATION)
            self.singletons[(zid, "managed_headers")] = copy.deepcopy(MANAGED_HEADERS)
            # Every zone lists the managed rulesets it can deploy. cf-terraforming
            # must leave them out (they are read-only for customers).
            self.rulesets[("zones", zid)] = [self._managed_ruleset()]
        for zone, rtype, name, content in FIXTURE_RECORDS:
            self.add_record(zone, rtype, name, content)

        marker_zone = self.zone_by_name(MARKER_ZONE)["id"]
        self.rulesets[("zones", marker_zone)].append(self._ruleset(
            f"zones/{marker_zone}", "default", "zone", "http_request_firewall_custom",
            [self._rule(f"zones/{marker_zone}/custom", "block",
                        '(http.request.uri.path contains "/wp-login.php")',
                        "Block WordPress logins")]))
        custom = self._ruleset(
            f"accounts/{acct}", "Shared firewall rules", "custom", "http_request_firewall_custom",
            [self._rule(f"accounts/{acct}/shared", "block",
                        '(ip.src.country in {"T1"})', "Block Tor exit nodes")],
            description="Executed by the account entry point")
        self.rulesets[("accounts", acct)] = [
            self._managed_ruleset(),
            self._ruleset(f"accounts/{acct}", "root", "root", "http_request_firewall_custom",
                          [self._rule(f"accounts/{acct}/root", "execute",
                                      '(cf.zone.name in {"alpha.example" "bravo.example" '
                                      '"charlie.example"})',
                                      "Run the shared rules", {"id": custom["id"]})]),
            custom,
        ]
        self.kv_namespaces = [{"id": ident("kv", title), "title": title,
                               "supports_url_encoding": True} for title in KV_NAMESPACES]
        tunnel_id = uuid_ident("tunnel", TUNNEL_NAME)
        self.tunnels = [{
            "id": tunnel_id, "account_tag": acct, "name": TUNNEL_NAME,
            "created_at": created, "deleted_at": None, "connections": [],
            "conns_active_at": None, "conns_inactive_at": created, "tun_type": "cfd_tunnel",
            "status": "inactive", "remote_config": True, "config_src": "cloudflare",
            "metadata": {},
        }]
        self.tunnel_configs = {tunnel_id: {
            "tunnel_id": tunnel_id, "version": 1, "source": "cloudflare", "created_at": created,
            "config": {
                "ingress": [
                    {"hostname": "app.charlie.example", "service": "https://app:8443",
                     "originRequest": {"noTLSVerify": True}},
                    {"service": "http_status:404"},
                ],
                "originRequest": {"connectTimeout": 30, "httpHostHeader": "app.internal"},
            },
        }}
        # Load balancing: monitor and pool belong to the account, the load
        # balancer that uses them to charlie.example.
        monitor = {
            "id": ident("monitor", "origin health"), "created_on": created,
            "modified_on": created, "type": "https", "method": "GET", "path": "/health",
            "expected_codes": "200", "expected_body": "", "description": "Origin health",
            "interval": 60, "retries": 2, "timeout": 5, "port": 0, "follow_redirects": False,
            "allow_insecure": False, "probe_zone": "", "header": {},
            "consecutive_up": 0, "consecutive_down": 0,
        }
        pool = {
            "id": ident("pool", "charlie-origins"), "created_on": created,
            "modified_on": created, "name": "charlie-origins", "description": "",
            "enabled": True, "minimum_origins": 1, "monitor": monitor["id"],
            "check_regions": None, "networks": ["cloudflare"], "notification_email": "",
            "origins": [
                {"name": "origin-1", "address": "192.0.2.10", "enabled": True, "weight": 1,
                 "header": {"Host": ["charlie.example"]}},
                {"name": "origin-2", "address": "192.0.2.11", "enabled": True, "weight": 0.5},
            ],
        }
        balancer = {
            "id": ident("load_balancer", "lb.charlie.example"), "created_on": created,
            "modified_on": created, "name": "lb.charlie.example", "description": "",
            "enabled": True, "proxied": False, "ttl": 30, "default_pools": [pool["id"]],
            "fallback_pool": pool["id"], "steering_policy": "off", "session_affinity": "none",
            "session_affinity_attributes": {"drain_duration": 0, "samesite": "Auto",
                                            "secure": "Auto", "zero_downtime_failover": "none"},
            "adaptive_routing": {"failover_across_pools": False},
            "location_strategy": {"mode": "pop", "prefer_ecs": "proximity"},
            "random_steering": {"default_weight": 1}, "pop_pools": {}, "region_pools": {},
            "networks": ["cloudflare"], "zone_name": MARKER_ZONE,
        }
        self.r2_buckets = [{"name": "roundtrip-assets", "creation_date": created,
                            "location": "weur", "storage_class": "Standard",
                            "jurisdiction": "default"}]
        # Generic collections: (name, zone or account id) -> objects with an "id".
        self.collections: dict[tuple[str, str], list[dict]] = {
            ("load_balancer_monitors", acct): [monitor],
            ("load_balancer_pools", acct): [pool],
            ("load_balancers", marker_zone): [balancer],
        }
        self.journal: list[dict] = []
        # What the tamper changed: key -> journal seq at the tamper.
        self.tampered: dict[str, int] = {}

    # -- builders ---------------------------------------------------------------
    @staticmethod
    def _setting(setting_id: str, value) -> dict:
        setting = {"id": setting_id, "value": copy.deepcopy(value), "editable": True,
                   "modified_on": None}
        if setting_id == "development_mode":
            setting["time_remaining"] = 0
        return setting

    @staticmethod
    def _rule(scope: str, action: str, expression: str, description: str,
              action_parameters: dict | None = None, ref: str | None = None) -> dict:
        rule_id = ident("rule", scope, ref or description)
        rule = {"id": rule_id, "version": "1", "action": action, "expression": expression,
                "description": description, "last_updated": now(), "ref": ref or rule_id,
                "enabled": True}
        if action_parameters:
            rule["action_parameters"] = action_parameters
        return rule

    @classmethod
    def _managed_ruleset(cls) -> dict:
        """A managed ruleset has one id everywhere it is listed."""
        return cls._ruleset("managed", "Cloudflare Managed Ruleset", "managed",
                            "http_request_firewall_managed", [],
                            description="Cloudflare's managed WAF rules")

    @staticmethod
    def _ruleset(scope: str, name: str, kind: str, phase: str, rules: list[dict],
                 description: str = "") -> dict:
        return {"id": ident("ruleset", scope, kind, phase, name), "name": name,
                "description": description, "kind": kind, "version": "1",
                "last_updated": now(), "phase": phase, "rules": rules}

    # -- lookups ----------------------------------------------------------------
    def zone_by_name(self, name: str) -> dict:
        for zone in self.zones:
            if zone["name"] == name:
                return zone
        raise ApiError(404, 1001, f"unknown zone {name!r}")

    def zone_by_id(self, zone_id: str, path: str) -> dict:
        for zone in self.zones:
            if zone["id"] == zone_id:
                return zone
        raise not_routable(path)

    def check_account(self, account_id: str, path: str) -> None:
        if account_id != self.account["id"]:
            raise not_routable(path)

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

    def find_record(self, zone_name: str, rtype: str, name: str) -> dict:
        zone = self.zone_by_name(zone_name)
        for rec in self.records[zone["id"]]:
            if rec["type"] == rtype and rec["name"] == fqdn(name, zone_name):
                return rec
        raise ApiError(404, 81044, f"no {rtype} record {name!r} in {zone_name}")

    def ruleset(self, container: tuple[str, str], ruleset_id: str) -> dict:
        for ruleset in self.rulesets.get(container, []):
            if ruleset["id"] == ruleset_id:
                return ruleset
        raise ApiError(404, 20000, "Could not find ruleset")

    def custom_zone_ruleset(self, zone_name: str) -> dict:
        container = ("zones", self.zone_by_name(zone_name)["id"])
        for ruleset in self.rulesets[container]:
            if ruleset["kind"] == "zone" and ruleset["phase"] == "http_request_firewall_custom":
                return ruleset
        raise ApiError(404, 20000, f"{zone_name} has no custom firewall ruleset")

    def kv_namespace(self, namespace_id: str) -> dict:
        for namespace in self.kv_namespaces:
            if namespace["id"] == namespace_id:
                return namespace
        raise ApiError(404, 10013, "namespace not found")

    def tunnel(self, tunnel_id: str) -> dict:
        for tunnel in self.tunnels:
            if tunnel["id"] == tunnel_id:
                return tunnel
        raise ApiError(404, 1003, "Tunnel not found")


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


def ok(result) -> dict:
    return {"success": True, "errors": [], "messages": [], "result": result}


def listing(items: list, query: dict) -> dict:
    chunk, info = paginate(items, query)
    payload = ok(copy.deepcopy(chunk))
    payload["result_info"] = info
    return payload


class Request:
    """What a route handler sees of one API request."""

    def __init__(self, method: str, path: str, query: dict, body, raw: bytes, headers=None):
        self.method, self.path, self.query, self.body, self.raw = method, path, query, body, raw
        self.headers = headers if headers is not None else {}
        self.dry_run = query.get("dry_run", [""])[0].lower() == "true"
        self.changed: bool | None = None  # set by handlers of writes

    def allow(self, *methods: str) -> None:
        if self.method not in methods:
            raise ApiError(405, 10405, f"Method {self.method} not allowed for this route")

    def json_object(self) -> dict:
        if not isinstance(self.body, dict):
            raise ApiError(400, 9207, "Request body is invalid JSON" if self.raw else
                           "Request body is missing")
        return self.body


# -- Routes -----------------------------------------------------------------------
# (name, resource type the route serves for the export or None, regex, handler).
# The name is what the journal records; mockctl.py checks that the backup asked
# every curated route at least once.
ROUTES: list[tuple[str, str | None, re.Pattern, object]] = []
ZONE = r"/zones/(?P<zone>[0-9a-f]{32})"
ACCOUNT = r"/accounts/(?P<account>[0-9a-f]{32})"
CONTAINER = r"/(?P<level>zones|accounts)/(?P<scope>[0-9a-f]{32})"


def route(name: str, pattern: str, resource_type: str | None = None):
    def register(handler):
        ROUTES.append((name, resource_type, re.compile(f"^{API}{pattern}$"), handler))
        return handler
    return register


@route("zones", r"/zones")
def h_zones(req: Request, store: Store) -> dict:
    req.allow("GET")
    zones = list(store.zones)
    if "account.id" in req.query:
        zones = [z for z in zones if z["account"]["id"] == req.query["account.id"][0]]
    if "name" in req.query:
        zones = [z for z in zones if z["name"] == req.query["name"][0]]
    return listing(zones, req.query)


@route("dns_records", ZONE + r"/dns_records", "cloudflare_dns_record")
def h_records(req: Request, store: Store, zone: str) -> dict:
    req.allow("GET")
    store.zone_by_id(zone, req.path)
    return listing(store.records[zone], req.query)


@route("dns_record", ZONE + r"/dns_records/(?P<record>[0-9a-f]{32})")
def h_record(req: Request, store: Store, zone: str, record: str) -> dict:
    req.allow("GET", "PUT", "PATCH")
    zone_name = store.zone_by_id(zone, req.path)["name"]
    rec = store.record(zone, record)
    if req.method != "GET":
        body = req.json_object()
        if req.method == "PUT":
            for key in ("name", "type"):
                if not isinstance(body.get(key), str) or not body[key]:
                    raise ApiError(400, 9000, f"DNS record {key} is required")
        if "ttl" in body and not isinstance(body["ttl"], (int, float)):
            raise ApiError(400, 9000, "DNS record ttl must be a number")
        before = semantic(rec)
        for key in RECORD_FIELDS:
            if key in body:
                rec[key] = body[key]
        rec["name"] = fqdn(rec["name"], zone_name)
        rec["proxiable"] = rec["type"] in PROXIABLE_TYPES
        rec["modified_on"] = now()
        req.changed = semantic(rec) != before
    return ok(copy.deepcopy(rec))


@route("zone_setting", ZONE + r"/settings/(?P<setting>[a-z0-9_]+)", "cloudflare_zone_setting")
def h_setting(req: Request, store: Store, zone: str, setting: str) -> dict:
    req.allow("GET", "PATCH")
    store.zone_by_id(zone, req.path)
    current = store.settings[zone].get(setting)
    if current is None:
        raise ApiError(404, 1003, f"Invalid or missing zone setting: {setting}")
    if req.method == "PATCH":
        body = req.json_object()
        if "value" not in body:
            raise ApiError(400, 1007, "Invalid value for zone setting: value is required")
        before = semantic(current)
        current["value"] = body["value"]
        if "enabled" in body:
            current["enabled"] = body["enabled"]
        current["modified_on"] = now()
        req.changed = semantic(current) != before
    return ok(copy.deepcopy(current))


@route("rulesets", CONTAINER + r"/rulesets", "cloudflare_ruleset")
def h_rulesets(req: Request, store: Store, level: str, scope: str) -> dict:
    """The phase list without rules, in one page like the real API."""
    req.allow("GET")
    _container(store, level, scope, req.path)
    return ok([{k: v for k, v in rs.items() if k != "rules"}
               for rs in store.rulesets.get((level, scope), [])])


@route("ruleset", CONTAINER + r"/rulesets/(?P<ruleset>[0-9a-f]{32})")
def h_ruleset(req: Request, store: Store, level: str, scope: str, ruleset: str) -> dict:
    req.allow("GET", "PUT")
    container = _container(store, level, scope, req.path)
    current = store.ruleset(container, ruleset)
    if req.method == "GET":
        return ok(copy.deepcopy(current))
    if current["kind"] == "managed":
        raise ApiError(403, 20004, "managed rulesets are read-only")
    updated = _updated_ruleset(current, req.json_object())
    req.changed = ruleset_semantic(updated) != ruleset_semantic(current)
    if not req.dry_run:
        current.clear()
        current.update(updated)
    return ok(copy.deepcopy(updated))


def _container(store: Store, level: str, scope: str, path: str) -> tuple[str, str]:
    if level == "zones":
        store.zone_by_id(scope, path)
    else:
        store.check_account(scope, path)
    return (level, scope)


def _updated_ruleset(current: dict, body: dict) -> dict:
    """The ruleset after a PUT: rules are matched to existing ones by id, then
    by ref; new rules get a new id."""
    for key in ("kind", "name", "phase"):
        if key in body and body[key] != current[key]:
            raise ApiError(400, 20021, f"the {key} of a ruleset cannot be changed")
    rules = body.get("rules", current["rules"])
    if not isinstance(rules, list):
        raise ApiError(400, 20021, "rules must be a list")
    by_id = {r["id"]: r for r in current["rules"]}
    by_ref = {r["ref"]: r for r in current["rules"]}
    stamp = now()
    new_rules = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ApiError(400, 20021, f"rules[{index}] must be an object")
        if rule.get("action") not in RULE_ACTIONS:
            raise ApiError(400, 20021, f"rules[{index}].action is not a known action")
        if not isinstance(rule.get("expression"), str) or not rule["expression"]:
            raise ApiError(400, 20021, f"rules[{index}].expression is required")
        previous = by_id.get(rule.get("id") or "") or by_ref.get(rule.get("ref") or "")
        rule_id = previous["id"] if previous else ident("rule", current["id"], str(index), stamp)
        new = {"id": rule_id, "action": rule["action"], "expression": rule["expression"],
               "description": rule.get("description") or "",
               "ref": rule.get("ref") or (previous["ref"] if previous else rule_id),
               "enabled": rule.get("enabled", True)}
        for key in RULE_OPTIONAL:
            if rule.get(key) not in (None, {}, []):
                new[key] = rule[key]
        unchanged = previous is not None and (
            {k: v for k, v in semantic(previous).items() if k != "id"}
            == {k: v for k, v in new.items() if k != "id"})
        new["version"] = previous["version"] if unchanged else (
            str(int(previous["version"]) + 1) if previous else "1")
        new["last_updated"] = previous["last_updated"] if unchanged else stamp
        new_rules.append(new)
    updated = copy.deepcopy(current)
    updated["rules"] = new_rules
    updated["description"] = body.get("description", current["description"]) or ""
    updated["version"] = str(int(current["version"]) + 1)
    updated["last_updated"] = stamp
    return updated


def _singleton(name: str, resource_type: str, *writes: str):
    @route(name, ZONE + f"/{name}", resource_type)
    def handler(req: Request, store: Store, zone: str) -> dict:
        req.allow("GET", *writes)
        store.zone_by_id(zone, req.path)
        current = store.singletons[(zone, name)]
        if req.method != "GET":
            before = semantic(current)
            _merge_singleton(name, current, req.json_object())
            req.changed = semantic(current) != before
        return ok(copy.deepcopy(current))
    return handler


def _merge_singleton(name: str, current: dict, body: dict) -> None:
    if name != "managed_headers":
        current.update(body)
        return
    for key in ("managed_request_headers", "managed_response_headers"):
        wanted = {h.get("id"): h.get("enabled") for h in body.get(key) or []}
        for header in current[key]:
            if header["id"] in wanted:
                header["enabled"] = bool(wanted[header["id"]])


_singleton("bot_management", "cloudflare_bot_management", "PUT")
_singleton("url_normalization", "cloudflare_url_normalization_settings", "PUT")
_singleton("managed_headers", "cloudflare_managed_transforms", "PATCH")


@route("kv_namespaces", ACCOUNT + r"/storage/kv/namespaces", "cloudflare_workers_kv_namespace")
def h_kv_namespaces(req: Request, store: Store, account: str) -> dict:
    req.allow("GET")
    store.check_account(account, req.path)
    return listing(store.kv_namespaces, req.query)


@route("kv_namespace", ACCOUNT + r"/storage/kv/namespaces/(?P<namespace>[0-9a-f]{32})")
def h_kv_namespace(req: Request, store: Store, account: str, namespace: str) -> dict:
    req.allow("GET", "PUT")
    store.check_account(account, req.path)
    current = store.kv_namespace(namespace)
    if req.method == "PUT":
        title = req.json_object().get("title")
        if not isinstance(title, str) or not title:
            raise ApiError(400, 10019, "title is required")
        req.changed = title != current["title"]
        current["title"] = title
    return ok(copy.deepcopy(current))


@route("r2_buckets", ACCOUNT + r"/r2/buckets", "cloudflare_r2_bucket")
def h_r2_buckets(req: Request, store: Store, account: str) -> dict:
    """The bucket list, wrapped in {"buckets": [...]} like the real API."""
    req.allow("GET")
    store.check_account(account, req.path)
    return ok({"buckets": copy.deepcopy(store.r2_buckets)})


@route("r2_bucket", ACCOUNT + r"/r2/buckets/(?P<bucket>[a-z0-9][a-z0-9-]{1,61}[a-z0-9])")
def h_r2_bucket(req: Request, store: Store, account: str, bucket: str) -> dict:
    req.allow("GET", "PATCH")
    store.check_account(account, req.path)
    current = next((b for b in store.r2_buckets if b["name"] == bucket), None)
    if current is None:
        raise ApiError(404, 10006, "The specified bucket does not exist.")
    if req.method == "PATCH":
        before = semantic(current)
        storage_class = (req.json_object().get("storage_class")
                         or req.headers.get("cf-r2-storage-class"))
        if storage_class:
            current["storage_class"] = storage_class
        req.changed = semantic(current) != before
    return ok(copy.deepcopy(current))


@route("tunnels", ACCOUNT + r"/cfd_tunnel", "cloudflare_zero_trust_tunnel_cloudflared")
def h_tunnels(req: Request, store: Store, account: str) -> dict:
    req.allow("GET")
    store.check_account(account, req.path)
    tunnels = store.tunnels
    if req.query.get("is_deleted", [""])[0].lower() == "false":
        tunnels = [t for t in tunnels if not t["deleted_at"]]
    return listing(tunnels, req.query)


@route("tunnel", ACCOUNT + r"/cfd_tunnel/(?P<tunnel>[0-9a-f-]{36})")
def h_tunnel(req: Request, store: Store, account: str, tunnel: str) -> dict:
    req.allow("GET", "PATCH")
    store.check_account(account, req.path)
    current = store.tunnel(tunnel)
    if req.method == "PATCH":
        body = req.json_object()
        before = semantic(current)
        for key in ("name", "config_src"):
            if key in body:
                current[key] = body[key]
        req.changed = semantic(current) != before
    return ok(copy.deepcopy(current))


@route("tunnel_config", ACCOUNT + r"/cfd_tunnel/(?P<tunnel>[0-9a-f-]{36})/configurations",
       "cloudflare_zero_trust_tunnel_cloudflared_config")
def h_tunnel_config(req: Request, store: Store, account: str, tunnel: str) -> dict:
    req.allow("GET", "PUT")
    store.check_account(account, req.path)
    store.tunnel(tunnel)
    current = store.tunnel_configs[tunnel]
    if req.method == "PUT":
        body = req.json_object()
        if not isinstance(body.get("config"), dict):
            raise ApiError(400, 1056, "config is required")
        before = semantic(current)
        current["config"] = body["config"]
        current["version"] += 1
        req.changed = semantic(current) != before
    return ok(copy.deepcopy(current))


def _collection(name: str, item: str, resource_type: str, prefix: str, suffix: str) -> None:
    """A list route and an item route (GET, PUT, PATCH) over
    ``Store.collections[(name, zone or account id)]``."""

    def scope_id(req: Request, store: Store, scope: dict) -> str:
        if "zone" in scope:
            store.zone_by_id(scope["zone"], req.path)
            return scope["zone"]
        store.check_account(scope["account"], req.path)
        return scope["account"]

    @route(name, prefix + re.escape(suffix), resource_type)
    def list_handler(req: Request, store: Store, **scope: str) -> dict:
        req.allow("GET")
        return listing(store.collections.get((name, scope_id(req, store, scope)), []), req.query)

    @route(item, prefix + re.escape(suffix) + r"/(?P<item_id>[0-9a-f]{32})")
    def item_handler(req: Request, store: Store, item_id: str, **scope: str) -> dict:
        req.allow("GET", "PUT", "PATCH")
        objects = store.collections.get((name, scope_id(req, store, scope)), [])
        current = next((o for o in objects if o["id"] == item_id), None)
        if current is None:
            raise ApiError(404, 1002, f"{item} not found")
        if req.method != "GET":
            body = req.json_object()
            before = semantic(current)
            if req.method == "PUT":
                for key in [k for k in current if k not in COMPUTED_FIELDS]:
                    current.pop(key)
            current.update({k: v for k, v in body.items() if k not in COMPUTED_FIELDS})
            current["modified_on"] = now()
            req.changed = semantic(current) != before
        return ok(copy.deepcopy(current))


_collection("load_balancers", "load_balancer", "cloudflare_load_balancer", ZONE,
            "/load_balancers")
_collection("load_balancer_pools", "load_balancer_pool", "cloudflare_load_balancer_pool",
            ACCOUNT, "/load_balancers/pools")
_collection("load_balancer_monitors", "load_balancer_monitor",
            "cloudflare_load_balancer_monitor", ACCOUNT, "/load_balancers/monitors")


# Every other curated type: the endpoint cf-terraforming 0.27 lists it with,
# empty. (name, resource type, path below the zone or account, empty result).
EMPTY_ZONE_ROUTES = (
    ("page_rules", "cloudflare_page_rule", "/pagerules", []),
    ("filters", "cloudflare_filter", "/filters", []),
    ("custom_hostnames", "cloudflare_custom_hostname", "/custom_hostnames", []),
    ("certificate_packs", "cloudflare_certificate_pack", "/ssl/certificate_packs", []),
    ("spectrum_apps", "cloudflare_spectrum_application", "/spectrum/apps", []),
    ("workers_routes", "cloudflare_workers_route", "/workers/routes", []),
    ("snippets", "cloudflare_snippets", "/snippets", []),
    ("snippet_rules", "cloudflare_snippet_rules", "/snippets/snippet_rules", []),
)
EMPTY_ACCOUNT_ROUTES = (
    ("account_members", "cloudflare_account_member", "/members", []),
    ("account_subscriptions", "cloudflare_account_subscription", "/subscriptions", []),
    ("lists", "cloudflare_list", "/rules/lists", []),
    ("notification_policies", "cloudflare_notification_policy", "/alerting/v3/policies", []),
    ("queues", "cloudflare_queue", "/queues", []),
    ("turnstile_widgets", "cloudflare_turnstile_widget", "/challenges/widgets", []),
    ("access_apps", "cloudflare_zero_trust_access_application", "/access/apps", []),
    ("access_policies", "cloudflare_zero_trust_access_policy", "/access/policies", []),
    ("access_groups", "cloudflare_zero_trust_access_group", "/access/groups", []),
    ("access_service_tokens", "cloudflare_zero_trust_access_service_token",
     "/access/service_tokens", []),
    ("tunnel_routes", "cloudflare_zero_trust_tunnel_cloudflared_route", "/teamnet/routes", []),
    ("virtual_networks", "cloudflare_zero_trust_tunnel_cloudflared_virtual_network",
     "/teamnet/virtual_networks", []),
    ("gateway_rules", "cloudflare_zero_trust_gateway_policy", "/gateway/rules", []),
    ("web_analytics_sites", "cloudflare_web_analytics_site", "/rum/site_info/list", []),
)


def _empty(name: str, resource_type: str, prefix: str, suffix: str, empty) -> None:
    @route(name, prefix + re.escape(suffix), resource_type)
    def handler(req: Request, store: Store, **scope: str) -> dict:
        req.allow("GET")
        if "zone" in scope:
            store.zone_by_id(scope["zone"], req.path)
        else:
            store.check_account(scope["account"], req.path)
        if isinstance(empty, list):
            return listing([], req.query)
        return ok(copy.deepcopy(empty))


for _name, _rtype, _suffix, _result in EMPTY_ZONE_ROUTES:
    _empty(_name, _rtype, ZONE, _suffix, _result)
for _name, _rtype, _suffix, _result in EMPTY_ACCOUNT_ROUTES:
    _empty(_name, _rtype, ACCOUNT, _suffix, _result)

# Curated types that never reach the API: cf-terraforming 0.27 cannot list
# them without ids the plugin does not have, or has no v5 endpoint for them.
NOT_REQUESTED = {"cloudflare_authenticated_origin_pulls", "cloudflare_workers_cron_trigger",
                 "cloudflare_workers_script"}


def client_of(user_agent: str) -> str:
    """Which client of the image sent a request, from its User-Agent."""
    if user_agent.startswith("cloudflare-go/"):
        return "cf-terraforming legacy (cloudflare-go v0)"
    if "cf-terraforming" in user_agent:
        return "cf-terraforming"
    if user_agent.startswith("Python-urllib"):
        return "plugin"
    if "terraform-provider" in user_agent.lower() or "opentofu" in user_agent.lower():
        return "provider"
    return user_agent[:60] or "unknown"


class Handler(BaseHTTPRequestHandler):
    """The API (TLS) and the control plane (plain HTTP, 127.0.0.1) share it;
    ``control`` tells them apart."""

    server_version = "cf-mock/2"
    protocol_version = "HTTP/1.1"
    control = False

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
        plane = "control" if self.control else "api"
        sys.stdout.write("%s %s %s\n" % (self.log_date_time_string(), plane, fmt % args))

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
        if self.control:
            if url.path == CONTROL or url.path.startswith(CONTROL + "/"):
                self._control(method, url.path, raw)
            else:
                self._send(404, {"error": "the control plane serves /__mock only"})
            return
        self._api(method, url.path, url.query, raw)

    # -- Cloudflare API ---------------------------------------------------------
    def _api(self, method: str, path: str, query_string: str, raw: bytes) -> None:
        query = parse_qs(query_string)
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        req = Request(method, path, query, body, raw, self.headers)
        entry = {"method": method, "path": path, "query": query_string,
                 "client": client_of(self.headers.get("User-Agent") or ""),
                 "probe": self.headers.get("X-Mock-Probe") == "1", "route": None,
                 "auth": self._auth(), "dry_run": req.dry_run}
        if method in ("PUT", "PATCH", "POST", "DELETE"):
            entry["body"] = body
        name, handler, params = self._resolve(path)
        entry["route"] = name
        try:
            if entry["auth"] != "ok":
                raise ApiError(401, 10000, "Authentication error")
            if handler is None:
                raise ApiError(404, 7000, "No route for that URI")
            with STORE.lock:
                status, payload = 200, handler(req, STORE, **params)
        except ApiError as exc:
            status, code, message = exc.status, exc.code, exc.message
        except Exception as exc:  # noqa: BLE001 - a mock bug must show up as a 500
            traceback.print_exc()
            status, code, message = 500, 10001, f"mock error: {exc}"
        if status >= 400:
            payload = {"success": False, "errors": [{"code": code, "message": message}],
                       "messages": [], "result": None}
        entry["changed"] = req.changed
        with STORE.lock:
            entry["seq"] = len(STORE.journal) + 1
            entry["status"] = status
            STORE.journal.append(entry)
        self._send(status, payload)

    @staticmethod
    def _resolve(path: str):
        """``(route name, handler, path parameters)``, all None when no route
        matches."""
        for name, _rtype, pattern, handler in ROUTES:
            match = pattern.match(path)
            if match:
                return name, handler, match.groupdict()
        return None, None, {}

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
            return 200, {
                "account": STORE.account, "zones": STORE.zones,
                "records": {names[zid]: recs for zid, recs in STORE.records.items()},
                "settings": {names[zid]: s for zid, s in STORE.settings.items()},
                "rulesets": {f"{level}/{names.get(scope, scope)}": rulesets
                             for (level, scope), rulesets in STORE.rulesets.items()},
                "tampered": STORE.tampered,
                "routes": [{"name": name, "resource_type": rtype}
                           for name, rtype, _, _ in ROUTES],
                "not_requested": sorted(NOT_REQUESTED),
            }
        if (method, path) == ("GET", f"{CONTROL}/journal"):
            return 200, {"entries": STORE.journal}
        if (method, path) == ("POST", f"{CONTROL}/seed"):
            return 201, Handler._seed(str(body["marker"]))
        if (method, path) == ("POST", f"{CONTROL}/tamper"):
            return 200, Handler._tamper()
        raise ApiError(404, 0, f"no control route {method} {path}")

    @staticmethod
    def _seed(marker: str) -> dict:
        """The marker data the backup has to capture."""
        rec = STORE.add_record(MARKER_ZONE, MARKER_TYPE, MARKER_NAME, marker)
        ruleset = STORE.custom_zone_ruleset(MARKER_ZONE)
        rule = Store._rule(f"zones/{ruleset['id']}", "managed_challenge",
                           MARKER_RULE_EXPRESSION, marker, ref=MARKER_RULE_REF)
        ruleset["rules"] = [r for r in ruleset["rules"] if r["ref"] != MARKER_RULE_REF] + [rule]
        ruleset["version"] = str(int(ruleset["version"]) + 1)
        setting = STORE.settings[STORE.zone_by_name(MARKER_ZONE)["id"]][MARKER_SETTING]
        setting["value"] = SEEDED_SETTING
        setting["modified_on"] = now()
        return {"record": rec, "rule": rule, "setting": setting}

    @staticmethod
    def _tamper() -> dict:
        """Out-of-band changes, the way edits in the dashboard would make them."""
        seq = len(STORE.journal)
        rec = STORE.find_record(MARKER_ZONE, MARKER_TYPE, MARKER_NAME)
        rec["content"] = TAMPERED_CONTENT
        rec["modified_on"] = now()
        ruleset = STORE.custom_zone_ruleset(MARKER_ZONE)
        rule = next(r for r in ruleset["rules"] if r["ref"] == MARKER_RULE_REF)
        rule.update(description=TAMPERED_CONTENT, enabled=False, last_updated=now())
        ruleset["version"] = str(int(ruleset["version"]) + 1)
        setting = STORE.settings[STORE.zone_by_name(MARKER_ZONE)["id"]][MARKER_SETTING]
        setting["value"] = TAMPERED_SETTING
        setting["modified_on"] = now()
        STORE.tampered = {f"dns_record/{rec['id']}": seq, f"ruleset/{ruleset['id']}": seq,
                          f"zone_setting/{MARKER_SETTING}": seq}
        return {"record": rec, "rule": rule, "setting": setting}


class ControlHandler(Handler):
    control = True


def tls_context(directory: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(os.path.join(directory, "cert.pem"),
                            os.path.join(directory, "key.pem"))
    return context


def main() -> int:
    global STORE, TOKEN, PAGE_SIZE
    TOKEN = os.environ.get("MOCK_API_TOKEN", "")
    if len(TOKEN) < 16:
        print("MOCK_API_TOKEN must be set (16+ characters)", file=sys.stderr)
        return 2
    tls_dir = os.environ.get("MOCK_TLS_DIR", "/tls")
    if not os.path.isfile(os.path.join(tls_dir, "cert.pem")):
        print(f"no TLS certificate in {tls_dir} - run tests/backup-roundtrip/prepare.sh",
              file=sys.stderr)
        return 2
    PAGE_SIZE = int(os.environ.get("MOCK_PAGE_SIZE", "2"))
    port = int(os.environ.get("MOCK_PORT", "8443"))
    control_port = int(os.environ.get("MOCK_CONTROL_PORT", "8080"))
    STORE = Store()

    api = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 - compose network only
    # The handshake runs in the request thread, so one stalled client cannot
    # block the accept loop.
    api.socket = tls_context(tls_dir).wrap_socket(api.socket, server_side=True,
                                                  do_handshake_on_connect=False)
    control = ThreadingHTTPServer(("127.0.0.1", control_port), ControlHandler)
    for server in (api, control):
        server.daemon_threads = True
    threading.Thread(target=control.serve_forever, daemon=True).start()
    print(f"cf-mock listening on https://:{port}{API} and http://127.0.0.1:{control_port}"
          f"{CONTROL} (page size {PAGE_SIZE}, {len(STORE.zones)} zones, {len(ROUTES)} routes)",
          flush=True)
    api.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
