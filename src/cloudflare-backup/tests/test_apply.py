from __future__ import annotations

import pytest
from fakes import make_tofu_run

from backuphelper_cloudflare.apply import ApplyError, apply_export, secret_warnings, stage_apply_dir
from backuphelper_cloudflare.config import CloudflareConfig

ENV = {"CLOUDFLARE_API_TOKEN": "t"}


def _tree(tmp_path):
    root = tmp_path / "tree"
    (root / "zones" / "a.com").mkdir(parents=True)
    (root / "main.tf").write_text('provider "cloudflare" {}\n')
    (root / "zones" / "a.com" / "cloudflare_dns_record.tf").write_text("resource x {}\n")
    (root / "zones" / "a.com" / "imports.tf").write_text("import { to = x id = 1 }\n")
    return root


def test_stage_single_zone_includes_imports(tmp_path):
    root = _tree(tmp_path)
    dest = tmp_path / "wd"
    scope, types = stage_apply_dir(root, dest, zone_slug="a.com")
    assert scope.name == "a.com"
    assert types == ["cloudflare_dns_record"]
    assert (dest / "main.tf").exists()
    assert (dest / "imports.tf").exists()


def test_stage_dr_omits_imports(tmp_path):
    root = _tree(tmp_path)
    dest = tmp_path / "wd"
    stage_apply_dir(root, dest, zone_slug="a.com", dr=True)
    assert not (dest / "imports.tf").exists()
    assert (dest / "cloudflare_dns_record.tf").exists()


def test_missing_zone_raises(tmp_path):
    root = _tree(tmp_path)
    with pytest.raises(ApplyError):
        stage_apply_dir(root, tmp_path / "wd", zone_slug="nope.com")


def test_multi_scope_requires_explicit_scope(tmp_path):
    root = _tree(tmp_path)
    (root / "zones" / "b.org").mkdir(parents=True)
    (root / "zones" / "b.org" / "cloudflare_dns_record.tf").write_text("resource y {}\n")
    with pytest.raises(ApplyError):
        stage_apply_dir(root, tmp_path / "wd")  # ambiguous → must specify


def test_secret_warnings():
    warns = secret_warnings(["cloudflare_dns_record",
                             "cloudflare_zero_trust_access_service_token"])
    assert len(warns) == 1
    assert warns[0]["resource_type"] == "cloudflare_zero_trust_access_service_token"


def test_apply_plan_only_does_not_apply(tmp_path):
    root = _tree(tmp_path)
    cfg = CloudflareConfig.from_spec({"type": "cloudflare"})
    result = apply_export(root, cfg, zone_slug="a.com", plan_only=True,
                          env=ENV, run_tofu=make_tofu_run())
    assert result.applied is False
    assert result.scope == "a.com"


def test_apply_gated_by_confirm(tmp_path):
    root = _tree(tmp_path)
    cfg = CloudflareConfig.from_spec({"type": "cloudflare"})
    # confirm returns False → not applied
    r1 = apply_export(root, cfg, zone_slug="a.com", confirm=lambda _r: False,
                      env=ENV, run_tofu=make_tofu_run())
    assert r1.applied is False
    # confirm returns True → applied + re-plan captured
    r2 = apply_export(root, cfg, zone_slug="a.com", confirm=lambda _r: True,
                      env=ENV, run_tofu=make_tofu_run())
    assert r2.applied is True


def test_apply_force_skips_confirm(tmp_path):
    root = _tree(tmp_path)
    cfg = CloudflareConfig.from_spec({"type": "cloudflare"})
    result = apply_export(root, cfg, zone_slug="a.com", force=True,
                          env=ENV, run_tofu=make_tofu_run())
    assert result.applied is True
