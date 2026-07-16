"""Minimal Cloudflare API client — zone discovery only.

cf-terraforming exports resources but cannot list zones, so the source needs one
small API call to enumerate what to back up. Implemented with stdlib ``urllib``
to avoid adding a runtime dependency; the HTTP fetch is injectable so tests run
fully offline.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

# fetch(url, token) -> parsed JSON dict. Injected in tests.
Fetch = Callable[[str, str], dict]


class CloudflareAPIError(RuntimeError):
    """A Cloudflare API request failed or returned success=false."""


@dataclass(frozen=True)
class Zone:
    id: str
    name: str
    account_id: Optional[str] = None


def _urllib_fetch(url: str, token: str, *, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise CloudflareAPIError(f"HTTP {exc.code} from {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise CloudflareAPIError(f"cannot reach {url}: {exc.reason}") from exc


def discover_zones(
    api_base: str,
    token: str,
    *,
    account_id: Optional[str] = None,
    fetch: Fetch = _urllib_fetch,
    per_page: int = 50,
    max_pages: int = 1000,
) -> list[Zone]:
    """List every zone visible to the token (optionally scoped to an account).

    Paginates the ``/zones`` endpoint. Raises ``CloudflareAPIError`` on a
    transport error or an ``success: false`` envelope so a broken discovery
    surfaces as a source failure (partial snapshot) rather than an empty backup.
    """
    zones: list[Zone] = []
    account_filter = f"&account.id={account_id}" if account_id else ""
    for page in range(1, max_pages + 1):
        url = f"{api_base}/zones?per_page={per_page}&page={page}{account_filter}"
        payload = fetch(url, token)
        if not payload.get("success", False):
            errors = payload.get("errors")
            raise CloudflareAPIError(f"zone listing failed: {errors}")
        for item in payload.get("result", []) or []:
            acct = (item.get("account") or {}).get("id")
            zones.append(Zone(id=item["id"], name=item["name"], account_id=acct))
        info = payload.get("result_info") or {}
        total_pages = info.get("total_pages")
        if not total_pages or page >= total_pages:
            break
    return zones


def list_tunnel_ids(
    api_base: str,
    token: str,
    account_id: str,
    *,
    fetch: Fetch = _urllib_fetch,
    per_page: int = 50,
    max_pages: int = 1000,
) -> list[str]:
    """List non-deleted Cloudflare Tunnel ids for an account.

    Needed because tunnel-scoped child resources (e.g.
    ``cloudflare_zero_trust_tunnel_cloudflared_config``) cannot be swept —
    cf-terraforming must be handed each tunnel id via ``--resource-id``.
    """
    ids: list[str] = []
    for page in range(1, max_pages + 1):
        url = (f"{api_base}/accounts/{account_id}/cfd_tunnel"
               f"?is_deleted=false&per_page={per_page}&page={page}")
        payload = fetch(url, token)
        if not payload.get("success", False):
            raise CloudflareAPIError(f"tunnel listing failed: {payload.get('errors')}")
        for item in payload.get("result", []) or []:
            if not item.get("deleted_at") and item.get("id"):
                ids.append(item["id"])
        info = payload.get("result_info") or {}
        total_pages = info.get("total_pages")
        if not total_pages or page >= total_pages:
            break
    return ids
