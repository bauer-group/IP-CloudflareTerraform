from __future__ import annotations

import json

from fakes import make_cf_run, make_fetch, make_tofu_run, zone_page

from backuphelper_cloudflare.config import CloudflareConfig
from backuphelper_cloudflare.export import EXPORT_MANIFEST_NAME, export

ENV = {"CLOUDFLARE_API_TOKEN": "test-token"}


def _cfg(**over):
    base = {
        "type": "cloudflare",
        "account_id": "acct1",
        "resource_types": "cloudflare_dns_record,cloudflare_ruleset",
        "account_resource_types": "cloudflare_list",
    }
    base.update(over)
    return CloudflareConfig.from_spec(base)


def _run(cfg, tmp_path, *, hcl, schema, zones, sleeps=None, fail=None):
    sleeps = sleeps if sleeps is not None else []
    return export(
        cfg, tmp_path / "out", env=ENV,
        run_tofu=make_tofu_run(schema),
        run_cf=make_cf_run(hcl, fail_types=fail or set()),
        fetch=make_fetch([zone_page(zones)]),
        sleep=lambda s: sleeps.append(s),
    )


def test_export_writes_expected_layout(tmp_path):
    cfg = _cfg()
    schema = {"cloudflare_dns_record", "cloudflare_ruleset", "cloudflare_list"}
    hcl = {"cloudflare_dns_record": 'resource "cloudflare_dns_record" "x" {}',
           "cloudflare_list": 'resource "cloudflare_list" "y" {}'}  # ruleset empty → skipped
    zones = [("z1", "a.com", "acct1"), ("z2", "b.org", "acct1")]
    sleeps = []
    result = _run(cfg, tmp_path, hcl=hcl, schema=schema, zones=zones, sleeps=sleeps)

    out = tmp_path / "out"
    assert (out / "main.tf").exists()
    assert (out / "zones" / "a.com" / "cloudflare_dns_record.tf").exists()
    assert (out / "zones" / "b.org" / "cloudflare_dns_record.tf").exists()
    assert (out / "_account" / "acct1" / "cloudflare_list.tf").exists()
    # ruleset returned empty HCL → no file
    assert not (out / "zones" / "a.com" / "cloudflare_ruleset.tf").exists()
    # import blocks captured alongside
    assert (out / "zones" / "a.com" / "imports.tf").exists()

    assert result.zone_count == 2
    assert result.files_written == 3  # dns×2 zones + list×1 account
    assert result.errors == []
    assert sleeps, "throttle sleep should be called between API calls"


def test_export_manifest_has_versions_and_counts(tmp_path):
    cfg = _cfg()
    schema = {"cloudflare_dns_record", "cloudflare_list"}
    hcl = {"cloudflare_dns_record": "resource x {}", "cloudflare_list": "resource y {}"}
    _run(cfg, tmp_path, hcl=hcl, schema=schema, zones=[("z1", "a.com", "acct1")])
    manifest = json.loads((tmp_path / "out" / EXPORT_MANIFEST_NAME).read_text())
    assert manifest["zone_count"] == 1
    assert manifest["tool_versions"]["provider_version"].startswith(">= 5.8.2")
    assert "OpenTofu" in manifest["tool_versions"]["tofu_version"]


def test_unknown_type_skipped_against_schema(tmp_path):
    cfg = _cfg(resource_types="cloudflare_dns_record,cloudflare_bogus", account_resource_types="")
    schema = {"cloudflare_dns_record"}  # bogus not in schema
    hcl = {"cloudflare_dns_record": "resource x {}"}
    result = _run(cfg, tmp_path, hcl=hcl, schema=schema, zones=[("z1", "a.com", "acct1")])
    assert "cloudflare_bogus" in result.skipped_unknown
    assert not (tmp_path / "out" / "zones" / "a.com" / "cloudflare_bogus.tf").exists()


def test_cf_failure_is_partial_not_fatal(tmp_path):
    cfg = _cfg(account_resource_types="")
    schema = {"cloudflare_dns_record", "cloudflare_ruleset"}
    hcl = {"cloudflare_dns_record": "resource x {}", "cloudflare_ruleset": "resource r {}"}
    result = _run(cfg, tmp_path, hcl=hcl, schema=schema,
                  zones=[("z1", "a.com", "acct1")], fail={"cloudflare_ruleset"})
    assert any("cloudflare_ruleset" in e for e in result.errors)
    assert (tmp_path / "out" / "zones" / "a.com" / "cloudflare_dns_record.tf").exists()
    assert result.files_written == 1


def test_benign_failure_is_skipped_not_error(tmp_path):
    # "No resource IDs defined" (empty / needs explicit ids) → skipped, not an error.
    cfg = _cfg(resource_types="cloudflare_dns_record,cloudflare_zone_setting",
               account_resource_types="")
    result = export(
        cfg, tmp_path / "out", env=ENV,
        run_tofu=make_tofu_run({"cloudflare_dns_record", "cloudflare_zone_setting"}),
        run_cf=make_cf_run({"cloudflare_dns_record": "resource x {}"},
                           benign_types={"cloudflare_zone_setting"}),
        fetch=make_fetch([zone_page([("z1", "a.com", "acct1")])]), sleep=lambda s: None)
    assert any("cloudflare_zone_setting" in s for s in result.skipped)
    assert not any("cloudflare_zone_setting" in e for e in result.errors)
    assert result.files_written == 1  # dns_record still exported


def test_secrets_report_populated(tmp_path):
    cfg = _cfg(resource_types="",
               account_resource_types="cloudflare_zero_trust_access_service_token")
    schema = {"cloudflare_zero_trust_access_service_token"}
    hcl = {"cloudflare_zero_trust_access_service_token": "resource s {}"}
    result = _run(cfg, tmp_path, hcl=hcl, schema=schema, zones=[("z1", "a.com", "acct1")])
    assert result.secrets_report
    assert result.secrets_report[0]["resource_type"] == "cloudflare_zero_trust_access_service_token"


def test_zone_setting_uses_default_resource_ids(tmp_path):
    # cloudflare_zone_setting can't be swept — export must pass --resource-id
    # with the built-in setting id list, and write the file.
    cfg = _cfg(resource_types="cloudflare_zone_setting", account_resource_types="")
    record: list = []
    export(cfg, tmp_path / "out", env=ENV,
           run_tofu=make_tofu_run({"cloudflare_zone_setting"}),
           run_cf=make_cf_run({"cloudflare_zone_setting": "resource x {}"}, record=record),
           fetch=make_fetch([zone_page([("z1", "a.com", "acct1")])]), sleep=lambda s: None)
    gen = [a for a in record if len(a) > 1 and a[1] == "generate"][0]
    val = gen[gen.index("--resource-id") + 1]
    assert val.startswith("cloudflare_zone_setting=")
    assert "ssl" in val and "min_tls_version" in val
    assert (tmp_path / "out" / "zones" / "a.com" / "cloudflare_zone_setting.tf").exists()


def test_tunnel_config_uses_dynamic_resource_ids(tmp_path):
    # cloudflare_zero_trust_tunnel_cloudflared_config is keyed by tunnel id;
    # export must fetch tunnel ids from the API and pass them via --resource-id.
    cfg = _cfg(resource_types="", resource_scope="account",
               account_resource_types="cloudflare_zero_trust_tunnel_cloudflared_config")
    tunnels = {"success": True, "result": [{"id": "t1", "name": "a"}, {"id": "t2", "name": "b"}],
               "result_info": {"total_pages": 1}}
    record: list = []
    result = export(
        cfg, tmp_path / "out", env=ENV,
        run_tofu=make_tofu_run({"cloudflare_zero_trust_tunnel_cloudflared_config"}),
        run_cf=make_cf_run({"cloudflare_zero_trust_tunnel_cloudflared_config": "resource x {}"},
                           record=record),
        fetch=make_fetch([tunnels]), sleep=lambda s: None)
    gen = [a for a in record if len(a) > 1 and a[1] == "generate"][0]
    assert gen[gen.index("--resource-id") + 1] == \
        "cloudflare_zero_trust_tunnel_cloudflared_config=t1,t2"
    assert result.files_written == 1


def test_tunnel_config_no_tunnels_is_skip(tmp_path):
    cfg = _cfg(resource_types="", resource_scope="account",
               account_resource_types="cloudflare_zero_trust_tunnel_cloudflared_config")
    empty = {"success": True, "result": [], "result_info": {"total_pages": 1}}
    result = export(
        cfg, tmp_path / "out", env=ENV,
        run_tofu=make_tofu_run({"cloudflare_zero_trust_tunnel_cloudflared_config"}),
        run_cf=make_cf_run({}), fetch=make_fetch([empty]), sleep=lambda s: None)
    assert any("no parent resources" in s for s in result.skipped)
    assert result.errors == []


def test_schema_both_scope_finds_content_at_secondary(tmp_path):
    # Lossless: a type whose content is at account is still found even though the
    # name heuristic tries zone first (no _ACCOUNT_NAME_HINTS match).
    cfg = _cfg(resource_types="", account_resource_types="",
               resource_discovery="schema", resource_scope="all")
    result = export(
        cfg, tmp_path / "out", env=ENV,
        run_tofu=make_tofu_run({"cloudflare_widget_thing"}),
        run_cf=make_cf_run({"cloudflare_widget_thing": "resource x {}"},
                           content_scope={"cloudflare_widget_thing": "account"}),
        fetch=make_fetch([zone_page([("z1", "a.com", "acct1")])]), sleep=lambda s: None)
    assert (tmp_path / "out" / "_account" / "acct1" / "cloudflare_widget_thing.tf").exists()
    assert not (tmp_path / "out" / "zones" / "a.com" / "cloudflare_widget_thing.tf").exists()
    assert result.errors == []


def test_schema_dual_scope_writes_both(tmp_path):
    # Dual-scope types (ruleset) export at BOTH scopes — no dedup.
    cfg = _cfg(resource_types="", account_resource_types="",
               resource_discovery="schema", resource_scope="all")
    export(cfg, tmp_path / "out", env=ENV,
           run_tofu=make_tofu_run({"cloudflare_ruleset"}),
           run_cf=make_cf_run({"cloudflare_ruleset": "resource x {}"}),  # content at both
           fetch=make_fetch([zone_page([("z1", "a.com", "acct1")])]), sleep=lambda s: None)
    assert (tmp_path / "out" / "zones" / "a.com" / "cloudflare_ruleset.tf").exists()
    assert (tmp_path / "out" / "_account" / "acct1" / "cloudflare_ruleset.tf").exists()


def test_schema_non_dual_stops_at_primary(tmp_path):
    # Efficiency: an account-hinted type with content at account is NOT retried at zone.
    cfg = _cfg(resource_types="", account_resource_types="",
               resource_discovery="schema", resource_scope="all")
    record: list = []
    export(cfg, tmp_path / "out", env=ENV,
           run_tofu=make_tofu_run({"cloudflare_workers_script"}),
           run_cf=make_cf_run({"cloudflare_workers_script": "resource x {}"}, record=record),
           fetch=make_fetch([zone_page([("z1", "a.com", "acct1"), ("z2", "b.org", "acct1")])]),
           sleep=lambda s: None)
    gens = [a for a in record if len(a) > 1 and a[1] == "generate"]
    assert gens and all("-a" in a for a in gens)  # account only
    assert not any("-z" in a for a in gens)


def test_export_passes_absolute_tofu_binary_path(tmp_path):
    # Regression: cf-terraforming --terraform-binary-path must be absolute
    # (it stats the literal value, it does not search PATH).
    cfg = _cfg(resource_types="cloudflare_dns_record", account_resource_types="",
               tofu_binary="/usr/local/bin/tofu")
    record: list = []
    export(cfg, tmp_path / "out", env=ENV,
           run_tofu=make_tofu_run({"cloudflare_dns_record"}),
           run_cf=make_cf_run({"cloudflare_dns_record": "resource x {}"}, record=record),
           fetch=make_fetch([zone_page([("z1", "a.com", "acct1")])]),
           sleep=lambda s: None)
    cf_calls = [a for a in record if "--terraform-binary-path" in a]
    assert cf_calls, "cf-terraforming should have been invoked"
    for argv in cf_calls:
        assert argv[argv.index("--terraform-binary-path") + 1] == "/usr/local/bin/tofu"


def test_zone_selection_filters(tmp_path):
    cfg = _cfg(zones="a.com", account_resource_types="")
    schema = {"cloudflare_dns_record"}
    hcl = {"cloudflare_dns_record": "resource x {}"}
    _run(cfg, tmp_path, hcl=hcl, schema=schema,
         zones=[("z1", "a.com", "acct1"), ("z2", "b.org", "acct1")])
    out = tmp_path / "out"
    assert (out / "zones" / "a.com" / "cloudflare_dns_record.tf").exists()
    assert not (out / "zones" / "b.org").exists()
