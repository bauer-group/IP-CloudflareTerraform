"""Open a stored snapshot's Cloudflare HCL export (for diff / apply / drift).

Reuses the engine's own front-half — off-site S3 hydration, the sha256 integrity
gate, decryption and deterministic extraction — so snapshot access can never
diverge from how the engine wrote it. Yields the extracted ``.tf`` tree (the
inner ``cloudflare.tar.gz`` unpacked).
"""

from __future__ import annotations

import json
import logging
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from backuphelper.archive.bundle import extract_bundle
from backuphelper.cli import data_dir, find_artifact, list_snapshots
from backuphelper.config.loader import load_config
from backuphelper.integrity.hashing import sha256_file
from backuphelper.runner import _decrypt_if_needed, _hydrate_from_destinations, remote_snapshot_ids

log = logging.getLogger(__name__)


class SnapshotError(RuntimeError):
    """A snapshot could not be located, verified, or opened."""


def _pick_job():
    cfg = load_config()
    return cfg.jobs[0] if cfg.jobs else None


def _component_archive(extracted: Path, component: Optional[str]) -> Path:
    """Find the Cloudflare component's nested ``<name>.tar.gz`` in an extracted
    outer bundle, using the embedded manifest to identify it by kind."""
    manifest_path = extracted / "manifest.json"
    name = component
    if name is None and manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for comp in manifest.get("components", []):
                if comp.get("kind") == "cloudflare" and not comp.get("error"):
                    name = comp.get("name")
                    break
        except (ValueError, OSError):
            pass
    if name is not None:
        candidate = extracted / f"{name}.tar.gz"
        if candidate.exists():
            return candidate
    archives = sorted(extracted.glob("*.tar.gz"))
    if len(archives) == 1:
        return archives[0]
    raise SnapshotError(
        f"could not locate the cloudflare component archive in the snapshot "
        f"(found {[p.name for p in archives]})")


@contextmanager
def open_export(snapshot_id: str, *, component: Optional[str] = None,
                verify: bool = True) -> Iterator[Path]:
    """Yield a path to the extracted HCL tree of ``snapshot_id``.

    Pulls the snapshot from the off-site S3 target first when it is not on the
    local volume, verifies its sha256 against the manifest, decrypts if needed,
    then extracts the outer bundle and the inner cloudflare component.
    """
    dd = data_dir()
    job = _pick_job()
    if job is not None:
        _hydrate_from_destinations(job, dd, snapshot_id)
    artifact = find_artifact(dd, snapshot_id)
    sidecar = dd / f"{snapshot_id}.manifest.json"
    if artifact is None or not sidecar.exists():
        raise SnapshotError(f"snapshot {snapshot_id} not found (artifact or manifest missing)")

    if verify:
        expected = json.loads(sidecar.read_text(encoding="utf-8")).get("archive_sha256")
        if expected and sha256_file(artifact) != expected:
            raise SnapshotError(f"snapshot {snapshot_id} failed its sha256 integrity check")

    with tempfile.TemporaryDirectory(prefix=f"cf-snap-{snapshot_id}-") as td:
        work = Path(td)
        bundle = _decrypt_if_needed(artifact, work)
        extracted = extract_bundle(bundle, work / "outer")
        inner = _component_archive(extracted, component)
        tree = extract_bundle(inner, work / "tree")
        yield tree


def latest_snapshot_id(exclude: Optional[str] = None) -> Optional[str]:
    """Newest snapshot id (local + off-site), excluding ``exclude`` if given.

    Snapshot ids are UTC ``%Y-%m-%d_%H-%M-%S`` strings, so lexical sort == chronological.
    """
    dd = data_dir()
    ids = {sid for sid, _ in list_snapshots(dd)}
    job = _pick_job()
    if job is not None:
        ids |= remote_snapshot_ids(job)
    ids.discard(exclude)
    return max(ids) if ids else None
