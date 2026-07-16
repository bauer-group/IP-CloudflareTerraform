from __future__ import annotations

import pytest

from backuphelper_cloudflare.config import CloudflareConfig, ConfigError


def test_defaults_and_from_spec_minimal():
    cfg = CloudflareConfig.from_spec({"type": "cloudflare"})
    assert cfg.name == "cloudflare"
    assert cfg.resource_scope == "all"
    assert cfg.resource_discovery == "curated"
    assert cfg.provider_version.startswith(">= 5.8.2")
    assert cfg.throttle_rps == 4.0
    assert cfg.modern_import_block is True


def test_from_spec_overrides_and_coercion():
    cfg = CloudflareConfig.from_spec({
        "type": "cloudflare", "name": "cf", "account_id": "acct1",
        "zones": "a.com, b.org", "resource_scope": "zone",
        "resource_types": "cloudflare_dns_record,cloudflare_ruleset",
        "throttle_rps": "2", "modern_import_block": "false", "validate": "yes",
    })
    assert cfg.name == "cf"
    assert cfg.account_id == "acct1"
    assert cfg.zone_selection == ["a.com", "b.org"]
    assert cfg.resource_types == ["cloudflare_dns_record", "cloudflare_ruleset"]
    assert cfg.throttle_rps == 2.0
    assert cfg.modern_import_block is False
    assert cfg.validate is True


def test_zone_selection_auto_is_none():
    assert CloudflareConfig.from_spec({"type": "cloudflare"}).zone_selection is None
    assert CloudflareConfig.from_spec({"type": "cloudflare", "zones": "auto"}).zone_selection is None


def test_invalid_scope_and_discovery_and_throttle():
    with pytest.raises(ConfigError):
        CloudflareConfig.from_spec({"type": "cloudflare", "resource_scope": "bogus"})
    with pytest.raises(ConfigError):
        CloudflareConfig.from_spec({"type": "cloudflare", "resource_discovery": "bogus"})
    with pytest.raises(ConfigError):
        CloudflareConfig.from_spec({"type": "cloudflare", "throttle_rps": "0"})


def test_resolve_token_prefers_env():
    cfg = CloudflareConfig.from_spec({"type": "cloudflare", "api_token": "spec-token"})
    assert cfg.resolve_token({"CLOUDFLARE_API_TOKEN": "env-token"}) == "env-token"
    assert cfg.resolve_token({}) == "spec-token"


def test_resolve_token_missing_raises():
    cfg = CloudflareConfig.from_spec({"type": "cloudflare"})
    with pytest.raises(ConfigError):
        cfg.resolve_token({})


def test_resource_ids_parsed_from_csv_and_list():
    cfg = CloudflareConfig.from_spec({
        "type": "cloudflare",
        "resource_ids": {"cloudflare_zone_setting": "ssl,brotli", "x": ["a", "b"]},
    })
    assert cfg.resource_ids["cloudflare_zone_setting"] == ["ssl", "brotli"]
    assert cfg.resource_ids["x"] == ["a", "b"]


def test_resource_ids_default_empty():
    assert CloudflareConfig.from_spec({"type": "cloudflare"}).resource_ids == {}


def test_tofu_binary_path_absolute_passthrough():
    cfg = CloudflareConfig.from_spec({"type": "cloudflare", "tofu_binary": "/usr/local/bin/tofu"})
    assert cfg.tofu_binary_path() == "/usr/local/bin/tofu"


def test_tofu_binary_path_resolves_bare_name_via_which(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/bin/tofu" if name == "tofu" else None)
    cfg = CloudflareConfig.from_spec({"type": "cloudflare"})  # default tofu_binary="tofu"
    # cf-terraforming stats the literal path, so a bare name must be resolved.
    assert cfg.tofu_binary_path() == "/opt/bin/tofu"
