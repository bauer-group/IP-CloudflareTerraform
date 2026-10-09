from __future__ import annotations

import pytest

from backuphelper_cloudflare.resources import (
    ACCOUNT_RESOURCE_TYPES,
    ZONE_RESOURCE_TYPES,
    classify_scope,
    curated_types,
)

# The list (or, without one, get) endpoint cf-terraforming 0.27.0 uses for each
# curated type with provider v5 (internal/app/cf-terraforming/cmd/
# resource_to_endpoint_mapping.go). cf-terraforming fills {zone_id} under -z and
# {account_id} under -a and leaves the other one empty, so a type exported
# under the wrong flag requests e.g. /accounts//load_balancers/pools and never
# yields anything. None: the type has no v5 endpoint there (nothing to export).
ENDPOINTS: dict[str, str | None] = {
    "cloudflare_dns_record": "/zones/{zone_id}/dns_records",
    "cloudflare_zone_setting": "/zones/{zone_id}/settings/{setting_id}",
    "cloudflare_ruleset": "/{accounts_or_zones}/{account_or_zone_id}/rulesets",
    "cloudflare_page_rule": "/zones/{zone_id}/pagerules",
    "cloudflare_filter": "/zones/{zone_id}/filters",
    "cloudflare_load_balancer": "/zones/{zone_id}/load_balancers",
    "cloudflare_load_balancer_pool": "/accounts/{account_id}/load_balancers/pools",
    "cloudflare_load_balancer_monitor": "/accounts/{account_id}/load_balancers/monitors",
    "cloudflare_managed_transforms": "/zones/{zone_id}/managed_headers",
    "cloudflare_url_normalization_settings": "/zones/{zone_id}/url_normalization",
    "cloudflare_custom_hostname": "/zones/{zone_id}/custom_hostnames",
    "cloudflare_certificate_pack": "/zones/{zone_id}/ssl/certificate_packs",
    "cloudflare_authenticated_origin_pulls":
        "/zones/{zone_id}/origin_tls_client_auth/hostnames/{hostname}",
    "cloudflare_bot_management": "/zones/{zone_id}/bot_management",
    "cloudflare_spectrum_application": "/zones/{zone_id}/spectrum/apps",
    "cloudflare_web_analytics_site": "/accounts/{account_id}/rum/site_info/list",
    "cloudflare_workers_route": "/zones/{zone_id}/workers/routes",
    "cloudflare_snippets": "/zones/{zone_id}/snippets",
    "cloudflare_snippet_rules": "/zones/{zone_id}/snippets/snippet_rules",
    "cloudflare_account_member": "/accounts/{account_id}/members",
    "cloudflare_account_subscription": "/accounts/{account_id}/subscriptions",
    "cloudflare_list": "/accounts/{account_id}/rules/lists",
    "cloudflare_notification_policy": "/accounts/{account_id}/alerting/v3/policies",
    "cloudflare_workers_script": None,
    "cloudflare_workers_kv_namespace": "/accounts/{account_id}/storage/kv/namespaces",
    "cloudflare_workers_cron_trigger": "/accounts/{account_id}/workers/scripts/{script_name}/schedules",
    "cloudflare_queue": "/accounts/{account_id}/queues",
    "cloudflare_r2_bucket": "/accounts/{account_id}/r2/buckets",
    "cloudflare_turnstile_widget": "/accounts/{account_id}/challenges/widgets",
    "cloudflare_zero_trust_access_application":
        "/{accounts_or_zones}/{account_or_zone_id}/access/apps",
    "cloudflare_zero_trust_access_policy": "/accounts/{account_id}/access/policies",
    "cloudflare_zero_trust_access_group":
        "/{accounts_or_zones}/{account_or_zone_id}/access/groups",
    "cloudflare_zero_trust_access_service_token":
        "/{accounts_or_zones}/{account_or_zone_id}/access/service_tokens",
    "cloudflare_zero_trust_tunnel_cloudflared": "/accounts/{account_id}/cfd_tunnel",
    "cloudflare_zero_trust_tunnel_cloudflared_config":
        "/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
    "cloudflare_zero_trust_tunnel_cloudflared_route": "/accounts/{account_id}/teamnet/routes",
    "cloudflare_zero_trust_tunnel_cloudflared_virtual_network":
        "/accounts/{account_id}/teamnet/virtual_networks",
    "cloudflare_zero_trust_gateway_policy": "/accounts/{account_id}/gateway/rules",
}
EITHER = "/{accounts_or_zones}/"


def test_every_curated_type_has_a_known_endpoint():
    assert set(ZONE_RESOURCE_TYPES) | set(ACCOUNT_RESOURCE_TYPES) == set(ENDPOINTS)


@pytest.mark.parametrize("resource_type", ZONE_RESOURCE_TYPES)
def test_zone_types_use_zone_endpoints(resource_type):
    endpoint = ENDPOINTS[resource_type]
    assert endpoint is None or endpoint.startswith(("/zones/", EITHER)), endpoint


@pytest.mark.parametrize("resource_type", ACCOUNT_RESOURCE_TYPES)
def test_account_types_use_account_endpoints(resource_type):
    endpoint = ENDPOINTS[resource_type]
    assert endpoint is None or endpoint.startswith(("/accounts/", EITHER)), endpoint


def test_moved_types_are_exported_at_their_api_scope():
    pairs = set(curated_types("all"))
    for rtype in ("cloudflare_load_balancer_pool", "cloudflare_load_balancer_monitor",
                  "cloudflare_web_analytics_site"):
        assert (rtype, "account") in pairs and (rtype, "zone") not in pairs
        assert classify_scope(rtype) == "account"
    for rtype in ("cloudflare_snippets", "cloudflare_snippet_rules"):
        assert (rtype, "zone") in pairs and (rtype, "account") not in pairs
        assert classify_scope(rtype) == "zone"
