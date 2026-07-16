"""Restore = apply an exported HCL tree back to Cloudflare (plan-gated).

Operates on ONE scope at a time (a single zone, or an account) so the ``tofu
plan`` a human reviews is coherent — OpenTofu only reads ``.tf`` from the working
directory root, and mixing zones would be unreviewable. ``import{}`` blocks from
the backup reconcile live resources into state (drift-correction); ``--dr`` omits
them for a from-scratch recreate.

Engine-independent: it takes a tree ``Path`` + config and shells to ``tofu`` via
the injectable wrapper, so it unit-tests without the engine or a real account.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional

from . import tofu
from .config import CloudflareConfig
from .resources import SECRET_BEARING_TYPES

_TYPE_FROM_FILE = re.compile(r"^(cloudflare_[A-Za-z0-9_]+)\.tf$")


class ApplyError(RuntimeError):
    """A restore/apply could not be staged or executed."""


@dataclass
class ApplyResult:
    scope: str = ""
    resource_types: list[str] = field(default_factory=list)
    secret_warnings: list[dict] = field(default_factory=list)
    plan_text: str = ""
    applied: bool = False
    replan_text: str = ""


def _scope_dir(tree: Path, *, zone_slug: Optional[str], account_slug: Optional[str]) -> Path:
    tree = Path(tree)
    if zone_slug and account_slug:
        raise ApplyError("choose either a zone or an account scope, not both")
    if zone_slug:
        candidate = tree / "zones" / zone_slug
        if not candidate.is_dir():
            available = [p.name for p in (tree / "zones").glob("*") if p.is_dir()]
            raise ApplyError(f"zone {zone_slug!r} not in snapshot; available: {available}")
        return candidate
    if account_slug:
        candidate = tree / "_account" / account_slug
        if not candidate.is_dir():
            available = [p.name for p in (tree / "_account").glob("*") if p.is_dir()]
            raise ApplyError(f"account {account_slug!r} not in snapshot; available: {available}")
        return candidate
    # No explicit scope: accept it only if exactly one zone exists.
    zones = [p for p in (tree / "zones").glob("*") if p.is_dir()]
    if len(zones) == 1 and not any((tree / "_account").glob("*")):
        return zones[0]
    raise ApplyError(
        "snapshot spans multiple scopes — specify --zone <name> or --account <id> "
        f"(zones: {[p.name for p in zones]})")


def stage_apply_dir(tree: Path, dest: Path, *, zone_slug: Optional[str] = None,
                    account_slug: Optional[str] = None, dr: bool = False) -> tuple[Path, list[str]]:
    """Copy the pinned provider + one scope's ``.tf`` into a flat working dir.

    Returns ``(scope_dir_name, resource_types)``. With ``dr=True`` the import
    blocks are omitted (fresh create instead of reconciling existing resources).
    """
    tree = Path(tree)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    scope = _scope_dir(tree, zone_slug=zone_slug, account_slug=account_slug)

    main_tf = tree / "main.tf"
    if main_tf.exists():
        shutil.copy2(main_tf, dest / "main.tf")

    resource_types: list[str] = []
    for tf in sorted(scope.glob("*.tf")):
        if tf.name == "imports.tf" and dr:
            continue
        shutil.copy2(tf, dest / tf.name)
        match = _TYPE_FROM_FILE.match(tf.name)
        if match:
            resource_types.append(match.group(1))
    return scope, resource_types


def secret_warnings(resource_types: list[str]) -> list[dict]:
    """Resources in this scope whose secret payload must be re-injected before apply."""
    return [
        {"resource_type": t, "lost_value": SECRET_BEARING_TYPES[t]}
        for t in sorted(set(resource_types)) if t in SECRET_BEARING_TYPES
    ]


def apply_export(
    tree: Path,
    cfg: CloudflareConfig,
    *,
    zone_slug: Optional[str] = None,
    account_slug: Optional[str] = None,
    dr: bool = False,
    force: bool = False,
    plan_only: bool = False,
    confirm: Optional[Callable[["ApplyResult"], bool]] = None,
    env: Optional[Mapping[str, str]] = None,
    workdir: Optional[Path] = None,
    run_tofu: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> ApplyResult:
    """Stage one scope, ``tofu plan`` it, gate on approval, then ``apply`` + re-plan.

    ``plan_only`` stops after the plan. ``force`` skips the ``confirm`` gate
    (unattended reconcile). Without ``force`` and with no ``confirm`` that
    returns True, the apply is not performed.
    """
    import tempfile

    token = cfg.resolve_token(env)
    proc_env = dict(env if env is not None else {})
    import os as _os
    proc_env = {**_os.environ, **proc_env, "CLOUDFLARE_API_TOKEN": token}

    created_tmp = workdir is None
    tmp = tempfile.mkdtemp(prefix="cf-apply-") if created_tmp else None
    dest = Path(tmp) if created_tmp else Path(workdir)
    try:
        scope, resource_types = stage_apply_dir(
            tree, dest, zone_slug=zone_slug, account_slug=account_slug, dr=dr)
        result = ApplyResult(scope=scope.name, resource_types=resource_types,
                             secret_warnings=secret_warnings(resource_types))

        tofu.init(dest, binary=cfg.tofu_binary, env=proc_env, timeout=cfg.timeout, run=run_tofu)
        plan = tofu.plan(dest, binary=cfg.tofu_binary, env=proc_env, out="tfplan",
                         timeout=cfg.timeout, run=run_tofu)
        result.plan_text = _stdout(plan)
        if plan_only:
            return result

        if not force:
            if confirm is None or not confirm(result):
                return result  # not approved → do not apply

        tofu.apply(dest, binary=cfg.tofu_binary, env=proc_env, plan_file="tfplan",
                   timeout=max(cfg.timeout, 1800), run=run_tofu)
        result.applied = True

        replan = tofu.plan(dest, binary=cfg.tofu_binary, env=proc_env,
                           timeout=cfg.timeout, run=run_tofu)
        result.replan_text = _stdout(replan)
        return result
    finally:
        if created_tmp and tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _stdout(result: subprocess.CompletedProcess) -> str:
    out = result.stdout
    return out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out or "")
