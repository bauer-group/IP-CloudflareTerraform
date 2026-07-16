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
    assert cft.benign_skip_reason('GET "https://api/zones/x/pagerules": 400 Bad Request')
    # real errors → None (surfaced as warnings)
    assert cft.benign_skip_reason("500 Internal Server Error") is None
    assert cft.benign_skip_reason("400 Bad Request on /some/other/endpoint") is None
    assert cft.benign_skip_reason("") is None
