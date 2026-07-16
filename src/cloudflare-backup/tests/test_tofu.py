from __future__ import annotations

import pytest
from fakes import make_tofu_run, proc

from backuphelper_cloudflare import tofu


def test_provider_config_pins_versions():
    hcl = tofu.provider_config(">= 5.8.2, < 6.0.0", ">= 1.11")
    assert 'source  = "cloudflare/cloudflare"' in hcl
    assert '">= 5.8.2, < 6.0.0"' in hcl
    assert 'required_version = ">= 1.11"' in hcl
    assert 'provider "cloudflare" {}' in hcl


def test_write_provider_config(tmp_path):
    tofu.write_provider_config(tmp_path, ">= 5.8.2, < 6.0.0", ">= 1.11")
    assert (tmp_path / "main.tf").read_text().count("cloudflare/cloudflare") == 1


def test_providers_schema_and_resource_types(tmp_path):
    run = make_tofu_run({"cloudflare_dns_record", "cloudflare_ruleset"})
    schema = tofu.providers_schema(tmp_path, env={}, run=run)
    assert tofu.provider_resource_types(schema) == {"cloudflare_dns_record", "cloudflare_ruleset"}


def test_init_raises_on_error(tmp_path):
    def run(argv, **kw):
        return proc(returncode=1, stderr=b"init boom")

    with pytest.raises(tofu.TofuError):
        tofu.init(tmp_path, env={}, run=run)


def test_version_parsing():
    run = make_tofu_run(version="OpenTofu v1.11.2")
    assert tofu.version(run=run) == "OpenTofu v1.11.2"
