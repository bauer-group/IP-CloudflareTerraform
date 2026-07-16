"""Curated Cloudflare provider-v5 resource-type catalog.

cf-terraforming has no "export everything" switch — you must name each
``--resource-type``. This module curates a sensible default allow-list split by
scope, plus the small stable set of account-scoped types (used to classify scope
in ``resource_discovery = "schema"`` mode) and the map of resources whose secret
payload does NOT round-trip through an export (for the secrets report).

These lists are DEFAULTS, tuned empirically per provider version. The exporter
additionally validates every candidate against the live provider schema
(``tofu providers schema``) and logs-and-skips anything the pinned provider does
not expose — so a stale name here degrades to a warning, never a broken backup.
Override per deployment with ``resource_types`` / ``account_resource_types`` /
``deny_types`` in the source config, or set ``resource_discovery = "schema"`` to
enumerate the whole provider.
"""

from __future__ import annotations

# Zone-scoped resources (cf-terraforming generate -z <zone_id>).
ZONE_RESOURCE_TYPES: tuple[str, ...] = (
    "cloudflare_dns_record",
    "cloudflare_zone_setting",
    "cloudflare_ruleset",
    "cloudflare_page_rule",
    "cloudflare_filter",
    "cloudflare_load_balancer",
    "cloudflare_load_balancer_pool",
    "cloudflare_load_balancer_monitor",
    "cloudflare_managed_transforms",
    "cloudflare_url_normalization_settings",
    "cloudflare_custom_hostname",
    "cloudflare_certificate_pack",
    "cloudflare_authenticated_origin_pulls",
    "cloudflare_bot_management",
    "cloudflare_spectrum_application",
    "cloudflare_web_analytics_site",
    "cloudflare_workers_route",
)

# Account-scoped resources (cf-terraforming generate -a <account_id>).
ACCOUNT_RESOURCE_TYPES: tuple[str, ...] = (
    "cloudflare_account_member",
    "cloudflare_account_subscription",
    "cloudflare_list",
    "cloudflare_ruleset",
    "cloudflare_notification_policy",
    "cloudflare_workers_script",
    "cloudflare_workers_kv_namespace",
    "cloudflare_workers_cron_trigger",
    "cloudflare_queue",
    "cloudflare_r2_bucket",
    "cloudflare_snippets",
    "cloudflare_snippet_rules",
    "cloudflare_turnstile_widget",
    "cloudflare_zero_trust_access_application",
    "cloudflare_zero_trust_access_policy",
    "cloudflare_zero_trust_access_group",
    "cloudflare_zero_trust_access_service_token",
    "cloudflare_zero_trust_tunnel_cloudflared",
    "cloudflare_zero_trust_gateway_policy",
)

# The stable set used to classify scope when enumerating the provider schema.
# Anything NOT in here is treated as zone-scoped; a misclassified new
# account-only resource simply errors under -z and is skipped (safe).
ACCOUNT_SCOPED_TYPES: frozenset[str] = frozenset(ACCOUNT_RESOURCE_TYPES) | {
    "cloudflare_account",
    "cloudflare_api_token",
    "cloudflare_account_token",
    "cloudflare_zero_trust_dlp_profile",
    "cloudflare_zero_trust_device_posture_rule",
    "cloudflare_zero_trust_list",
}

# Types known to emit HCL that does not round-trip cleanly or is not worth
# capturing by default. Merged with the per-deployment `deny_types`.
DEFAULT_DENY_TYPES: frozenset[str] = frozenset()

# Resources whose secret payload the Cloudflare API never returns, so a plain
# `apply` from a backup shows a spurious replace until the secret is re-supplied.
# value = the lost attribute, surfaced in the secrets report / SECRETS-MANIFEST.
SECRET_BEARING_TYPES: dict[str, str] = {
    "cloudflare_zero_trust_access_service_token": "client_secret (shown once at creation)",
    "cloudflare_zero_trust_tunnel_cloudflared": "tunnel_secret",
    "cloudflare_origin_ca_certificate": "private key material",
    "cloudflare_custom_ssl": "private key material",
    "cloudflare_mtls_certificate": "private key material",
    "cloudflare_api_token": "token value (minted anew on re-apply)",
    "cloudflare_account_token": "token value",
    "cloudflare_workers_secret": "secret text (write-only)",
}


def curated_types(scope: str) -> tuple[tuple[str, str], ...]:
    """Return ``(resource_type, scope_flag)`` pairs for the curated allow-list.

    ``scope`` is one of ``all`` / ``zone`` / ``account``; ``scope_flag`` is the
    cf-terraforming flag to use (``-z`` per zone, ``-a`` once for the account).
    """
    pairs: list[tuple[str, str]] = []
    if scope in ("all", "zone"):
        pairs += [(t, "zone") for t in ZONE_RESOURCE_TYPES]
    if scope in ("all", "account"):
        pairs += [(t, "account") for t in ACCOUNT_RESOURCE_TYPES]
    return tuple(pairs)


def classify_scope(resource_type: str) -> str:
    """Best-effort scope classification for a schema-discovered resource type."""
    return "account" if resource_type in ACCOUNT_SCOPED_TYPES else "zone"
