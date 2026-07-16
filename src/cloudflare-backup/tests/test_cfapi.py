from __future__ import annotations

import pytest
from fakes import make_fetch, zone_page

from backuphelper_cloudflare.cfapi import CloudflareAPIError, discover_zones


def test_discover_zones_single_page():
    fetch = make_fetch([zone_page([("z1", "a.com", "acct1"), ("z2", "b.org", "acct1")])])
    zones = discover_zones("https://api", "tok", fetch=fetch)
    assert [z.name for z in zones] == ["a.com", "b.org"]
    assert zones[0].id == "z1"
    assert zones[0].account_id == "acct1"


def test_discover_zones_paginates():
    fetch = make_fetch([
        zone_page([("z1", "a.com", "acct1")], page=1, total_pages=2),
        zone_page([("z2", "b.org", "acct1")], page=2, total_pages=2),
    ])
    zones = discover_zones("https://api", "tok", fetch=fetch)
    assert {z.name for z in zones} == {"a.com", "b.org"}


def test_discover_zones_error_envelope_raises():
    fetch = make_fetch([{"success": False, "errors": [{"message": "bad token"}]}])
    with pytest.raises(CloudflareAPIError):
        discover_zones("https://api", "tok", fetch=fetch)


def test_account_filter_in_url():
    seen = {}

    def fetch(url, token, **kw):
        seen["url"] = url
        return zone_page([("z1", "a.com", "acct9")])

    discover_zones("https://api", "tok", account_id="acct9", fetch=fetch)
    assert "account.id=acct9" in seen["url"]
