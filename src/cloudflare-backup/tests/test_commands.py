"""Command tests — require the engine (backuphelper) at import time.

Like the source tests they run inside the image build and skip on a host
without the engine.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

pytest.importorskip("backuphelper", reason="engine not installed on this host")

# The engine CLI first: it loads this plugin's command group itself.
import backuphelper.cli  # noqa: E402,F401
from typer.testing import CliRunner  # noqa: E402

from backuphelper_cloudflare import commands  # noqa: E402
from backuphelper_cloudflare import export as export_mod  # noqa: E402
from backuphelper_cloudflare.config import CloudflareConfig  # noqa: E402
from backuphelper_cloudflare.export import ExportResult  # noqa: E402


def _write(tree, rel, text):
    path = tree / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def drift(tmp_path, monkeypatch):
    """`cloudflare drift` against a baseline with two zones; returns a runner
    taking the zones the fresh export finds."""
    baseline = tmp_path / "baseline"
    _write(baseline, "main.tf", "terraform {}\n")
    _write(baseline, "_account/acct1/cloudflare_ruleset.tf", "resource r {}\n")
    _write(baseline, "zones/x.com/cloudflare_dns_record.tf", 'resource x {\n  ip = "1"\n}\n')
    _write(baseline, "zones/y.org/cloudflare_dns_record.tf", "resource y {}\n")

    @contextmanager
    def open_export(snapshot_id):
        yield baseline

    monkeypatch.setattr(commands, "latest_snapshot_id", lambda: "2026-01-01_00-00-00")
    monkeypatch.setattr(commands, "open_export", open_export)
    monkeypatch.setattr(commands, "_load_cfg", lambda: CloudflareConfig())

    def run(found_zones):
        def fake_export(cfg, out, **kwargs):
            _write(out, "main.tf", "terraform {}\n")
            _write(out, "_account/acct1/cloudflare_ruleset.tf", "resource r {}\n")
            for zone in found_zones:
                _write(out, f"zones/{zone}/cloudflare_dns_record.tf", 'resource x {\n  ip = "2"\n}\n')
            return ExportResult(zones=list(found_zones), zone_count=len(found_zones),
                                files_written=1 + len(found_zones))

        monkeypatch.setattr(export_mod, "export", fake_export)
        return CliRunner().invoke(commands.app, ["drift", "--zone", "x.com"])

    return run


def test_drift_zone_compares_only_that_zone(drift):
    result = drift(["x.com"])
    assert result.exit_code == 1, result.output
    assert "changed: 1  added: 0  removed: 0" in result.output
    assert "y.org" not in result.output


def test_drift_zone_the_token_cannot_see_is_an_error(drift):
    result = drift([])
    assert result.exit_code == 2, result.output
    assert "not visible to the token" in result.output
