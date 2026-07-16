"""The export core — live Cloudflare account → Terraform HCL tree.

Orchestrates one initialized OpenTofu working dir (so cf-terraforming can read
the provider schema once), enumerates zones, then loops the selected resource
types running ``cf-terraforming generate`` (+ ``import`` blocks) per scope,
writing a clean, ``tofu fmt``-canonicalized HCL tree plus an ``EXPORT_MANIFEST``.

Every subprocess and the HTTP fetch are injected, so the whole flow unit-tests
offline. Failures degrade to recorded warnings — the run produces a partial
snapshot rather than aborting.

Output layout (this tree becomes the bundled ``cloudflare.tar.gz`` component):

    main.tf                       # pinned provider (reused on restore)
    _account/<account_id>/<type>.tf
    _account/<account_id>/imports.tf
    zones/<zone_name>/<type>.tf
    zones/<zone_name>/imports.tf
    EXPORT_MANIFEST.json
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional

from . import cfapi, cfterraforming, tofu
from .config import CloudflareConfig
from .resources import (
    ACCOUNT_RESOURCE_TYPES,
    DEFAULT_DENY_TYPES,
    DYNAMIC_ID_TYPES,
    RESOURCE_ID_DEFAULTS,
    SECRET_BEARING_TYPES,
    ZONE_RESOURCE_TYPES,
    classify_scope,
    curated_types,
)

log = logging.getLogger(__name__)

EXPORT_MANIFEST_NAME = "EXPORT_MANIFEST.json"
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(name: str) -> str:
    """Filesystem-safe zone/account directory name (matches on-disk layout)."""
    return _SAFE.sub("_", name.strip()) or "unnamed"


# Internal alias kept for readability at call sites within this module.
_slug = slug


@dataclass
class ExportResult:
    zone_count: int = 0
    zones: list[str] = field(default_factory=list)
    account_ids: list[str] = field(default_factory=list)
    types_with_content: int = 0
    types_attempted: int = 0
    skipped_unknown: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # expected sweep outcomes (not errors)
    files_written: int = 0
    secrets_report: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    tool_versions: dict = field(default_factory=dict)

    def as_metadata(self) -> dict:
        """The compact subset folded into the engine's component manifest."""
        return {
            "zone_count": self.zone_count,
            "account_ids": self.account_ids,
            "types_with_content": self.types_with_content,
            "types_skipped_unknown": len(self.skipped_unknown),
            "types_skipped": len(self.skipped),
            "files_written": self.files_written,
            "has_secrets_to_reinject": bool(self.secrets_report),
            "errors": len(self.errors),
            **self.tool_versions,
        }


def _select_types(cfg: CloudflareConfig, schema_types: set[str]) -> list[tuple[str, str]]:
    """Return ``(resource_type, scope)`` pairs to export, honoring overrides,
    discovery mode, deny-list and the live provider schema."""
    pairs: list[tuple[str, str]]
    if cfg.resource_types or cfg.account_resource_types:
        pairs = []
        if cfg.resource_scope in ("all", "zone"):
            pairs += [(t, "zone") for t in (cfg.resource_types or ZONE_RESOURCE_TYPES)]
        if cfg.resource_scope in ("all", "account"):
            pairs += [(t, "account") for t in (cfg.account_resource_types or ACCOUNT_RESOURCE_TYPES)]
    elif cfg.resource_discovery == "schema" and schema_types:
        pairs = []
        for t in sorted(schema_types):
            scope = classify_scope(t)
            if cfg.resource_scope == "all" or cfg.resource_scope == scope:
                pairs.append((t, scope))
    else:
        pairs = list(curated_types(cfg.resource_scope))

    deny = set(DEFAULT_DENY_TYPES) | set(cfg.deny_types)
    return [(t, s) for (t, s) in pairs if t not in deny]


def _dynamic_ids(resource_type: str, scope: str, scope_id: str, cfg: CloudflareConfig,
                 token: str, fetch: cfapi.Fetch) -> list[str]:
    """Fetch the --resource-id list for a parent-keyed child type from the API."""
    if resource_type == "cloudflare_zero_trust_tunnel_cloudflared_config" and scope == "account":
        return cfapi.list_tunnel_ids(cfg.api_base, token, scope_id, fetch=fetch)
    return []


def _build_env(cfg: CloudflareConfig, token: str, base_env: Optional[Mapping[str, str]]) -> dict:
    env = dict(os.environ if base_env is None else base_env)
    env["CLOUDFLARE_API_TOKEN"] = token
    if cfg.account_id:
        env.setdefault("CLOUDFLARE_ACCOUNT_ID", cfg.account_id)
    return env


def export(
    cfg: CloudflareConfig,
    output_dir: Path,
    *,
    env: Optional[Mapping[str, str]] = None,
    run_tofu: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    run_cf: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    fetch: cfapi.Fetch = cfapi._urllib_fetch,
    sleep: Callable[[float], None] = time.sleep,
) -> ExportResult:
    """Export the account into ``output_dir`` and return an ``ExportResult``."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = ExportResult()
    token = cfg.resolve_token(env)
    proc_env = _build_env(cfg, token, env)
    throttle = 1.0 / cfg.throttle_rps if cfg.throttle_rps > 0 else 0.0
    # cf-terraforming stats --terraform-binary-path literally (no PATH search),
    # so it must be an absolute path, not a bare "tofu".
    tofu_bin_abs = cfg.tofu_binary_path()

    # A throwaway working dir keeps .terraform/ provider binaries out of the backup.
    with tempfile.TemporaryDirectory(prefix="cf-tofu-") as work:
        workdir = Path(work)
        tofu.write_provider_config(workdir, cfg.provider_version, cfg.required_version)
        # Reuse the pinned provider block on restore.
        (output_dir / "main.tf").write_text(
            tofu.provider_config(cfg.provider_version, cfg.required_version), encoding="utf-8"
        )
        tofu.init(workdir, binary=cfg.tofu_binary, env=proc_env, timeout=cfg.timeout, run=run_tofu)

        result.tool_versions = {
            "tofu_version": tofu.version(cfg.tofu_binary, run=run_tofu),
            "cf_terraforming_version": cfterraforming.version(cfg.cfterraforming_binary, run=run_cf),
            "provider_version": cfg.provider_version,
        }

        schema_types: set[str] = set()
        try:
            schema = tofu.providers_schema(workdir, binary=cfg.tofu_binary, env=proc_env,
                                           timeout=cfg.timeout, run=run_tofu)
            schema_types = tofu.provider_resource_types(schema)
        except Exception as exc:  # noqa: BLE001 - schema is a safety net, not required
            log.warning("could not read provider schema (no validation this run): %s", exc)
            result.errors.append(f"provider schema unavailable: {exc}")

        pairs = _select_types(cfg, schema_types)

        # Enumerate zones (needed for zone-scoped types and to infer account ids).
        zones: list[cfapi.Zone] = []
        needs_zones = cfg.resource_scope in ("all", "zone") or not cfg.account_id
        if needs_zones:
            try:
                zones = cfapi.discover_zones(cfg.api_base, token, account_id=cfg.account_id,
                                             fetch=fetch)
            except Exception as exc:  # noqa: BLE001 - degrade to account-only if possible
                log.error("zone discovery failed: %s", exc)
                result.errors.append(f"zone discovery failed: {exc}")
        selection = cfg.zone_selection
        if selection is not None:
            wanted = {s.lower() for s in selection}
            zones = [z for z in zones if z.name.lower() in wanted or z.id.lower() in wanted]
        result.zones = [z.name for z in zones]
        result.zone_count = len(zones)

        account_ids = ([cfg.account_id] if cfg.account_id
                       else sorted({z.account_id for z in zones if z.account_id}))
        result.account_ids = [a for a in account_ids if a]

        secret_types_seen: set[str] = set()

        def _emit(scope: str, scope_id: str, target_dir: Path, resource_type: str) -> None:
            result.types_attempted += 1
            if schema_types and resource_type not in schema_types:
                result.skipped_unknown.append(resource_type)
                log.info("skip %s: not in provider schema", resource_type)
                return
            # Types that cannot be swept (e.g. cloudflare_zone_setting) need
            # explicit ids: config override, else static defaults, else the
            # parent ids fetched from the API (e.g. tunnel config <- tunnel ids).
            ids = cfg.resource_ids.get(resource_type) or list(
                RESOURCE_ID_DEFAULTS.get(resource_type, ()))
            if not ids and resource_type in DYNAMIC_ID_TYPES:
                try:
                    ids = _dynamic_ids(resource_type, scope, scope_id, cfg, token, fetch)
                except Exception as exc:  # noqa: BLE001 - degrade, don't abort
                    result.errors.append(f"{resource_type}: id discovery failed: {exc}")
                    return
                if not ids:
                    result.skipped.append(
                        f"{resource_type} ({scope}={scope_id}): no parent resources")
                    return
            try:
                hcl = cfterraforming.generate(
                    binary=cfg.cfterraforming_binary, resource_type=resource_type, scope=scope,
                    scope_id=scope_id, install_path=workdir, tofu_binary=tofu_bin_abs,
                    env=proc_env, resource_ids=ids, timeout=cfg.timeout, run=run_cf)
            except cfterraforming.CfTerraformingError as exc:
                reason = cfterraforming.benign_skip_reason(exc.stderr)
                if reason:
                    result.skipped.append(f"{resource_type} ({scope}={scope_id}): {reason}")
                    log.info("skip %s (%s=%s): %s", resource_type, scope, scope_id, reason)
                else:
                    result.errors.append(str(exc))
                    log.warning("%s", exc)
                return
            finally:
                if throttle:
                    sleep(throttle)
            if not cfterraforming.has_content(hcl):
                return
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / f"{resource_type}.tf").write_text(hcl, encoding="utf-8")
            result.files_written += 1
            result.types_with_content += 1
            if resource_type in SECRET_BEARING_TYPES:
                secret_types_seen.add(resource_type)
            if cfg.modern_import_block:
                try:
                    blocks = cfterraforming.import_blocks(
                        binary=cfg.cfterraforming_binary, resource_type=resource_type, scope=scope,
                        scope_id=scope_id, install_path=workdir, tofu_binary=tofu_bin_abs,
                        env=proc_env, resource_ids=ids, modern_import_block=True,
                        timeout=cfg.timeout, run=run_cf)
                    if cfterraforming.has_content(blocks):
                        with (target_dir / "imports.tf").open("a", encoding="utf-8") as fh:
                            fh.write(blocks.rstrip() + "\n")
                except cfterraforming.CfTerraformingError as exc:
                    reason = cfterraforming.benign_skip_reason(exc.stderr)
                    if reason:
                        result.skipped.append(f"{resource_type} imports ({scope}={scope_id}): {reason}")
                    else:
                        result.errors.append(str(exc))
                finally:
                    if throttle:
                        sleep(throttle)

        for resource_type, scope in pairs:
            if scope == "zone":
                for zone in zones:
                    _emit("zone", zone.id, output_dir / "zones" / _slug(zone.name), resource_type)
            else:  # account
                for account_id in result.account_ids:
                    _emit("account", account_id, output_dir / "_account" / _slug(account_id),
                          resource_type)
                if not result.account_ids:
                    result.errors.append(
                        f"account-scoped {resource_type} skipped: no account id "
                        "(set account_id or grant the token account access)")

        tofu.fmt(output_dir, binary=cfg.tofu_binary, env=proc_env, run=run_tofu)

    result.secrets_report = [
        {"resource_type": t, "lost_value": SECRET_BEARING_TYPES[t]}
        for t in sorted(secret_types_seen)
    ]
    _write_manifest(output_dir, cfg, result)
    return result


def _write_manifest(output_dir: Path, cfg: CloudflareConfig, result: ExportResult) -> None:
    manifest = {
        "kind": "cloudflare-terraform-export",
        "schema_version": 1,
        "tool_versions": result.tool_versions,
        "provider_version": cfg.provider_version,
        "required_version": cfg.required_version,
        "resource_scope": cfg.resource_scope,
        "resource_discovery": cfg.resource_discovery,
        "zone_count": result.zone_count,
        "zones": sorted(result.zones),
        "account_ids": result.account_ids,
        "types_with_content": result.types_with_content,
        "types_attempted": result.types_attempted,
        "skipped_unknown": sorted(set(result.skipped_unknown)),
        "skipped": sorted(result.skipped),
        "files_written": result.files_written,
        "secrets_report": result.secrets_report,
        "errors": result.errors,
    }
    (output_dir / EXPORT_MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
