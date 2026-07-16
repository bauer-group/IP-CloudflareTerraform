from __future__ import annotations

import json

from backuphelper_cloudflare.diff import diff_trees
from backuphelper_cloudflare.export import EXPORT_MANIFEST_NAME


def _write(tree, rel, text):
    p = tree / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_no_changes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, "zones/x/cloudflare_dns_record.tf", "resource x {\n  a = 1\n}\n")
    _write(b, "zones/x/cloudflare_dns_record.tf", "resource x {\n  a = 1\n}\n")
    result = diff_trees(a, b)
    assert not result.has_changes


def test_changed_added_removed(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, "zones/x/dns.tf", "resource x {\n  ip = \"1.1.1.1\"\n}\n")
    _write(b, "zones/x/dns.tf", "resource x {\n  ip = \"2.2.2.2\"\n}\n")
    _write(a, "zones/x/old.tf", "resource old {}\n")
    _write(b, "zones/x/new.tf", "resource new {}\n")
    result = diff_trees(a, b)
    assert result.has_changes
    assert "zones/x/dns.tf" in result.changed
    assert "zones/x/old.tf" in result.removed
    assert "zones/x/new.tf" in result.added
    assert "2.2.2.2" in result.text


def test_normalization_ignores_whitespace(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, "x.tf", "resource x {\n  a = 1\n}\n")
    _write(b, "x.tf", "resource x {\n  a = 1\n}\n\n\n")  # extra trailing blanks
    assert not diff_trees(a, b).has_changes
    # raw mode DOES see the whitespace difference
    assert diff_trees(a, b, raw=True).has_changes


def test_version_gate_warns_on_mismatch(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, "x.tf", "resource x {}\n")
    _write(b, "x.tf", "resource x {}\n")
    (a / EXPORT_MANIFEST_NAME).write_text(json.dumps(
        {"tool_versions": {"provider_version": ">= 5.8.2, < 6.0.0",
                           "cf_terraforming_version": "v0.27.0", "tofu_version": "v1.11.0"}}))
    (b / EXPORT_MANIFEST_NAME).write_text(json.dumps(
        {"tool_versions": {"provider_version": ">= 5.9.0, < 6.0.0",
                           "cf_terraforming_version": "v0.27.0", "tofu_version": "v1.11.0"}}))
    result = diff_trees(a, b)
    assert result.version_warning and "provider_version" in result.version_warning
