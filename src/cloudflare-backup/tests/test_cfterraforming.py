from __future__ import annotations

from pathlib import Path

import pytest
from fakes import make_cf_run

from backuphelper_cloudflare import cfterraforming as cft


def test_generate_builds_correct_argv():
    record: list = []
    run = make_cf_run({"cloudflare_dns_record": "resource ..."}, record=record)
    out = cft.generate(binary="cf-terraforming", resource_type="cloudflare_dns_record",
                       scope="zone", scope_id="z1", install_path=Path("/wd"),
                       tofu_binary="tofu", env={"CLOUDFLARE_API_TOKEN": "t"}, run=run)
    assert out == "resource ..."
    argv = record[0]
    assert argv[:2] == ["cf-terraforming", "generate"]
    assert "--resource-type" in argv and "cloudflare_dns_record" in argv
    assert "-z" in argv and "z1" in argv
    assert "--terraform-binary-path" in argv and "tofu" in argv
    # install path is passed through as str(Path) — platform-native separator.
    assert argv[argv.index("--terraform-install-path") + 1] == str(Path("/wd"))


def test_account_scope_uses_dash_a():
    record: list = []
    run = make_cf_run({"cloudflare_list": "x"}, record=record)
    cft.generate(binary="cf-terraforming", resource_type="cloudflare_list", scope="account",
                 scope_id="acct1", install_path=Path("/wd"), tofu_binary="tofu",
                 env={}, run=run)
    assert "-a" in record[0] and "acct1" in record[0]


def test_generate_raises_on_failure():
    run = make_cf_run({}, fail_types={"cloudflare_dns_record"})
    with pytest.raises(cft.CfTerraformingError):
        cft.generate(binary="cf-terraforming", resource_type="cloudflare_dns_record",
                     scope="zone", scope_id="z1", install_path=Path("/wd"),
                     tofu_binary="tofu", env={}, run=run)


def test_generate_with_resource_ids_appends_flag():
    record: list = []
    run = make_cf_run({"cloudflare_zone_setting": "resource ..."}, record=record)
    cft.generate(binary="cf-terraforming", resource_type="cloudflare_zone_setting",
                 scope="zone", scope_id="z1", install_path=Path("/wd"), tofu_binary="/tofu",
                 env={}, resource_ids=["ssl", "brotli"], run=run)
    argv = record[0]
    assert argv[argv.index("--resource-id") + 1] == "cloudflare_zone_setting=ssl,brotli"


def test_generate_without_resource_ids_omits_flag():
    record: list = []
    run = make_cf_run({"cloudflare_dns_record": "x"}, record=record)
    cft.generate(binary="cf-terraforming", resource_type="cloudflare_dns_record",
                 scope="zone", scope_id="z1", install_path=Path("/wd"), tofu_binary="/tofu",
                 env={}, run=run)
    assert "--resource-id" not in record[0]


def test_import_blocks_modern_flag():
    record: list = []
    run = make_cf_run({"cloudflare_dns_record": "x"}, record=record)
    out = cft.import_blocks(binary="cf-terraforming", resource_type="cloudflare_dns_record",
                            scope="zone", scope_id="z1", install_path=Path("/wd"),
                            tofu_binary="tofu", env={}, modern_import_block=True, run=run)
    assert "import {" in out
    assert "--modern-import-block" in record[0]


def test_has_content():
    assert cft.has_content("resource x {}")
    assert not cft.has_content("\n  # just a comment\n\n")
    assert not cft.has_content("")


def test_benign_skip_reason():
    # expected sweep outcomes → a reason (treated as skip, not error)
    assert cft.benign_skip_reason("No resource IDs defined in Terraform for resource X")
    assert cft.benign_skip_reason("GET /zones/x/spectrum/apps: 403 Forbidden")
    assert cft.benign_skip_reason("404 not found")
    assert cft.benign_skip_reason("GET /zones/x/logs/control/retention/flag: 401 Unauthorized")
    assert cft.benign_skip_reason("GET /accounts/x/r2/buckets/%7Bbucket_name%7D/cors: 400 Bad Request")
    assert cft.benign_skip_reason("Could not route to /zones/{identifier}/subscription")
    assert cft.benign_skip_reason("No route for that URI")
    assert cft.benign_skip_reason('GET "https://api/zones/x/pagerules": 400 Bad Request')
    assert cft.benign_skip_reason("GET /zones/x/custom_certificates: 400 Bad Request")
    # real errors → None (surfaced as warnings: 5xx, 429, unexpected)
    assert cft.benign_skip_reason("500 Internal Server Error") is None
    assert cft.benign_skip_reason("429 Too Many Requests") is None
    assert cft.benign_skip_reason("connection reset by peer") is None
    assert cft.benign_skip_reason("") is None


def _imports(*pairs: tuple[str, str]) -> str:
    return "\n".join(f'import {{\n  to = {to}\n  id = "{id_}"\n}}\n' for to, id_ in pairs)


def test_reconcile_imports_follows_the_generated_resources():
    # cf-terraforming 0.27 + provider v5: generate drops managed rulesets and
    # sorts by phase, import lists every ruleset in API order.
    hcl = ('resource "cloudflare_ruleset" "terraform_managed_resource_bbb_0" {\n'
           '  kind = "zone"\n}\n\n'
           'resource "cloudflare_ruleset" "terraform_managed_resource_ccc_1" {\n'
           '  kind = "zone"\n}\n')
    imports = _imports(
        ("cloudflare_ruleset.terraform_managed_resource_aaa_0", "zones/z1/aaa"),  # managed
        ("cloudflare_ruleset.terraform_managed_resource_ccc_1", "zones/z1/ccc"),
        ("cloudflare_ruleset.terraform_managed_resource_bbb_2", "zones/z1/bbb"))
    blocks, dropped = cft.reconcile_imports("cloudflare_ruleset", hcl, imports)
    assert dropped == 1
    assert blocks == _imports(
        ("cloudflare_ruleset.terraform_managed_resource_ccc_1", "zones/z1/ccc"),
        ("cloudflare_ruleset.terraform_managed_resource_bbb_0", "zones/z1/bbb"))


def test_reconcile_imports_keeps_matching_blocks_and_ids_with_underscores():
    hcl = ('resource "cloudflare_zone_setting" "terraform_managed_resource_always_online_0" {}\n'
           'resource "cloudflare_zone_setting" "terraform_managed_resource_0rtt_1" {}\n')
    imports = _imports(
        ("cloudflare_zone_setting.terraform_managed_resource_always_online_0", "z1/always_online"),
        ("cloudflare_zone_setting.terraform_managed_resource_0rtt_1", "z1/0rtt"))
    assert cft.reconcile_imports("cloudflare_zone_setting", hcl, imports) == (imports, 0)


def test_reconcile_imports_leaves_unreadable_output_alone():
    imports = _imports(("cloudflare_dns_record.x", "abc"))
    assert cft.reconcile_imports("cloudflare_dns_record", "resource x {}", imports) == (imports, 0)


# An import block in a layout the rewrites do not read (an extra attribute).
OTHER_LAYOUT = ('import {\n  to       = cloudflare_ruleset.terraform_managed_resource_bbb_0\n'
                '  id       = "zones/z1/bbb"\n  provider = cloudflare.zone\n}\n')


def test_reconcile_imports_keeps_blocks_in_another_layout():
    # Rebuilding the file from the blocks it can read would lose this one.
    hcl = 'resource "cloudflare_ruleset" "terraform_managed_resource_bbb_0" {\n}\n'
    imports = _imports(
        ("cloudflare_ruleset.terraform_managed_resource_aaa_0", "zones/z1/aaa")) + OTHER_LAYOUT
    assert cft.reconcile_imports("cloudflare_ruleset", hcl, imports) == (imports, 0)


# cf-terraforming 0.27 output (hclwrite layout) for a zone's managed transforms.
MANAGED_TRANSFORMS = '''resource "cloudflare_managed_transforms" "terraform_managed_resource_z1_0" {
  zone_id = "z1"
  managed_request_headers = [{
    enabled = false
    id      = "add_bot_protection_headers"
    }, {
    enabled = true
    id      = "add_visitor_location_headers"
  }]
  managed_response_headers = [{
    conflicts_with = ["remove_x-powered-by_header"]
    enabled        = false
    id             = "add_security_headers"
  }]
}
'''


def test_managed_transforms_keep_only_enabled_ones():
    hcl = cft.adapt_to_provider("cloudflare_managed_transforms", MANAGED_TRANSFORMS)
    assert hcl == '''resource "cloudflare_managed_transforms" "terraform_managed_resource_z1_0" {
  zone_id = "z1"
  managed_request_headers = [{ enabled = true, id = "add_visitor_location_headers" }]
  managed_response_headers = []
}
'''


def test_empty_snippet_rules_are_dropped():
    empty = ('resource "cloudflare_snippet_rules" "terraform_managed_resource_z1_0" {\n'
             '  zone_id = "z1"\n  rules   = []\n}\n')
    assert not cft.has_content(cft.adapt_to_provider("cloudflare_snippet_rules", empty))
    rules = ('resource "cloudflare_snippet_rules" "terraform_managed_resource_z1_0" {\n'
             '  zone_id = "z1"\n  rules = [{\n    enabled      = true\n'
             '    expression   = "(http.request.full_uri wildcard \\"/hello\\")"\n'
             '    snippet_name = "hello"\n  }]\n}\n')
    assert cft.adapt_to_provider("cloudflare_snippet_rules", rules) == rules


def test_empty_monitor_header_is_dropped():
    monitor = ('resource "cloudflare_load_balancer_monitor" "terraform_managed_resource_m1_0" {\n'
               '  account_id = "acct1"\n  header           = {}\n  interval = 60\n}\n')
    assert cft.adapt_to_provider("cloudflare_load_balancer_monitor", monitor) == (
        'resource "cloudflare_load_balancer_monitor" "terraform_managed_resource_m1_0" {\n'
        '  account_id = "acct1"\n  interval = 60\n}\n')
    with_header = monitor.replace("header           = {}",
                                  'header = {\n    Host = ["example.com"]\n  }')
    assert cft.adapt_to_provider("cloudflare_load_balancer_monitor", with_header) == with_header


def test_pool_origin_host_header_uses_the_provider_key():
    pool = ('resource "cloudflare_load_balancer_pool" "terraform_managed_resource_p1_0" {\n'
            '  account_id = "acct1"\n  name       = "origins"\n  origins = [{\n'
            '    address = "192.0.2.10"\n    header = {\n      Host = ["example.com"]\n'
            '    }\n    name = "origin-1"\n  }]\n}\n')
    adapted = cft.adapt_to_provider("cloudflare_load_balancer_pool", pool)
    assert adapted == pool.replace('      Host = ["example.com"]', '      host = ["example.com"]')


def test_tunnel_config_keys_use_the_provider_names():
    rtype = "cloudflare_zero_trust_tunnel_cloudflared_config"
    # cf-terraforming 0.27 writes the API's keys (its own v5 fixture layout).
    hcl = (f'resource "{rtype}" "terraform_managed_resource_acct1_0" {{\n'
           '  account_id = "acct1"\n  tunnel_id  = "t1"\n  config = {\n'
           '    ingress = [{\n      hostname = "app.example.com"\n'
           '      originRequest = {\n        noTLSVerify = true\n      }\n'
           '      service = "https://app:8443"\n      }, {\n'
           '      service = "http_status:404"\n    }]\n'
           '    originRequest = {\n      access = {\n        audTag   = ["aud"]\n'
           '        required = true\n        teamName = "team"\n      }\n'
           '      connectTimeout = 30\n      httpHostHeader = "app.internal"\n    }\n  }\n}\n')
    adapted = cft.adapt_to_provider(rtype, hcl)
    for camel, snake in (("originRequest", "origin_request"), ("noTLSVerify", "no_tls_verify"),
                         ("audTag", "aud_tag"), ("teamName", "team_name"),
                         ("connectTimeout", "connect_timeout"),
                         ("httpHostHeader", "http_host_header")):
        assert f" {camel} " not in adapted and f" {snake} " in adapted
    # Values and the other keys are untouched.
    assert 'hostname = "app.example.com"' in adapted and "required = true" in adapted
    assert adapted.count("\n") == hcl.count("\n")


def test_other_types_are_not_adapted():
    assert cft.adapt_to_provider("cloudflare_dns_record", MANAGED_TRANSFORMS) == MANAGED_TRANSFORMS


def test_imports_from_attributes_builds_provider_ids():
    rtype = "cloudflare_zero_trust_tunnel_cloudflared_config"
    hcl = (f'resource "{rtype}" "terraform_managed_resource_acct1_0" {{\n'
           '  account_id = "acct1"\n  source     = "cloudflare"\n  tunnel_id  = "t1"\n'
           '  config = {\n    ingress = [{\n      service = "http_status:404"\n    }]\n  }\n}\n\n'
           f'resource "{rtype}" "terraform_managed_resource_acct1_1" {{\n'
           '  account_id = "acct1"\n}\n')
    blocks, missing = cft.imports_from_attributes(rtype, hcl, ("account_id", "tunnel_id"))
    assert blocks == _imports((f"{rtype}.terraform_managed_resource_acct1_0", "acct1/t1"))
    assert missing == ["terraform_managed_resource_acct1_1"]


def test_imports_from_attributes_uses_defaults():
    hcl = ('resource "cloudflare_r2_bucket" "terraform_managed_resource_acct1_0" {\n'
           '  account_id = "acct1"\n  name       = "assets"\n}\n\n'
           'resource "cloudflare_r2_bucket" "terraform_managed_resource_acct1_1" {\n'
           '  account_id   = "acct1"\n  jurisdiction = "eu"\n  name         = "logs"\n}\n')
    blocks, missing = cft.imports_from_attributes(
        "cloudflare_r2_bucket", hcl, ("account_id", "name", "jurisdiction=default"))
    assert blocks == _imports(
        ("cloudflare_r2_bucket.terraform_managed_resource_acct1_0", "acct1/assets/default"),
        ("cloudflare_r2_bucket.terraform_managed_resource_acct1_1", "acct1/logs/eu"))
    assert missing == []


def test_drop_imports_with_the_scope_id_twice():
    imports = _imports(
        ("cloudflare_turnstile_widget.terraform_managed_resource_acct1_0", "acct1/acct1"),
        ("cloudflare_list.terraform_managed_resource_l1_0", "acct1/l1"))
    blocks, dropped = cft.drop_imports_with_id(imports, "acct1/acct1")
    assert dropped == 1
    assert blocks == _imports(("cloudflare_list.terraform_managed_resource_l1_0", "acct1/l1"))
    assert cft.drop_imports_with_id(blocks, "acct1/acct1") == (blocks, 0)


def test_drop_imports_keeps_blocks_in_another_layout():
    imports = _imports(
        ("cloudflare_turnstile_widget.terraform_managed_resource_acct1_0", "acct1/acct1")
    ) + OTHER_LAYOUT
    assert cft.drop_imports_with_id(imports, "acct1/acct1") == (imports, 0)
