"""Source tests — require the engine (backuphelper) at import time.

They run inside the image build (where backuphelper is installed) and skip on a
host without it, so the engine-independent suite still runs everywhere.
"""

from __future__ import annotations

import pytest

pytest.importorskip("backuphelper", reason="engine not installed on this host")

from backuphelper_cloudflare import export as export_core  # noqa: E402
from backuphelper_cloudflare.export import ExportResult  # noqa: E402
from backuphelper_cloudflare.source import CloudflareSource  # noqa: E402


def _fake_export(files_written, *, errors=None):
    def _export(cfg, output_dir, **kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "main.tf").write_text('provider "cloudflare" {}\n')
        for i in range(files_written):
            z = output_dir / "zones" / f"z{i}"
            z.mkdir(parents=True, exist_ok=True)
            (z / "cloudflare_dns_record.tf").write_text("resource x {}\n")
        r = ExportResult(zone_count=files_written, files_written=files_written,
                         errors=errors or [])
        r.tool_versions = {"tofu_version": "OpenTofu v1.11.0"}
        return r

    return _export


def test_produce_bundles_component(tmp_path, monkeypatch):
    monkeypatch.setattr(export_core, "export", _fake_export(2))
    src = CloudflareSource({"type": "cloudflare", "api_token": "t"})
    components = src.produce(tmp_path)
    assert len(components) == 1
    comp = components[0]
    assert comp.error is None
    assert comp.name == "cloudflare"
    assert comp.kind == "cloudflare"
    assert comp.path is not None and comp.path.exists()
    assert comp.path.name == "cloudflare.tar.gz"
    assert comp.metadata["zone_count"] == 2


def test_produce_empty_is_error_component(tmp_path, monkeypatch):
    monkeypatch.setattr(export_core, "export",
                        _fake_export(0, errors=["zone discovery failed"]))
    src = CloudflareSource({"type": "cloudflare", "api_token": "t"})
    comp = src.produce(tmp_path)[0]
    assert comp.path is None
    assert comp.error and "no files" in comp.error


def test_produce_misconfig_is_error_component(tmp_path):
    # No token anywhere → ConfigError surfaced as an errored component, not a raise.
    src = CloudflareSource({"type": "cloudflare"})
    import os
    token = os.environ.pop("CLOUDFLARE_API_TOKEN", None)
    try:
        comp = src.produce(tmp_path)[0]
    finally:
        if token is not None:
            os.environ["CLOUDFLARE_API_TOKEN"] = token
    assert comp.path is None
    assert comp.error
