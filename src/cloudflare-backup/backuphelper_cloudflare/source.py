"""The ``cloudflare`` backup source (registered under ``backuphelper.sources``).

``produce`` runs the export core into a temp staging tree and packages it as one
deterministic ``cloudflare.tar.gz`` component (the same shape the built-in
``s3`` source uses). The engine then hashes it, folds it into the snapshot
bundle, optionally encrypts, uploads off-site and applies retention.

Restore is intentionally NOT implemented here: pushing HCL back to Cloudflare is
`tofu apply`, which is destructive and must be plan-gated — that lives in the
``cloudflare apply`` command verb, not the engine's automatic restore loop.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Mapping

from backuphelper.archive.bundle import create_bundle
from backuphelper.sources.base import Source, StagedComponent

from . import export as export_core
from .config import CloudflareConfig, ConfigError

log = logging.getLogger(__name__)


class CloudflareSource(Source):
    """Export the live Cloudflare account to Terraform HCL via cf-terraforming."""

    type = "cloudflare"

    def __init__(self, spec: Mapping[str, Any]):
        super().__init__(spec)
        self.cfg = CloudflareConfig.from_spec(spec)

    @property
    def component_name(self) -> str:
        return self.cfg.name

    def produce(self, staging_dir: Path) -> list[StagedComponent]:
        staging_dir = Path(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)
        out = staging_dir / f"{self.cfg.name}.tar.gz"
        try:
            with tempfile.TemporaryDirectory(dir=staging_dir, prefix="cf-export-") as td:
                tree = Path(td) / "export"
                result = export_core.export(self.cfg, tree)
                create_bundle(tree, out)
        except ConfigError as exc:
            return [self._error(out, f"cloudflare source misconfigured: {exc}")]
        except Exception as exc:  # noqa: BLE001 - degrade to a partial snapshot
            log.error("cloudflare export failed: %s", exc)
            return [self._error(out, f"cloudflare export failed: {exc}")]

        metadata = result.as_metadata()
        # A run that produced nothing at all is a failure, not a silent empty backup.
        if result.files_written == 0:
            detail = "; ".join(result.errors[:3]) or "no resources exported"
            out.unlink(missing_ok=True)
            return [self._error(out, f"cloudflare export produced no files: {detail}", metadata)]
        return [StagedComponent(name=self.cfg.name, kind=self.type, path=out, metadata=metadata)]

    def _error(self, out: Path, message: str, metadata: dict | None = None) -> StagedComponent:
        out.unlink(missing_ok=True)
        return StagedComponent(name=self.cfg.name, kind=self.type, path=None,
                               metadata=metadata or {}, error=message)
