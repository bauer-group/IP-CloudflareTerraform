"""Thin wrapper around the ``cf-terraforming`` binary.

``generate`` emits Terraform HCL for a resource type; ``import_blocks`` emits the
``import{}`` blocks (or ``terraform import`` commands) that bind live resources
into state. Both drive OpenTofu via ``--terraform-binary-path`` and read the
provider schema from an already-initialized working dir
(``--terraform-install-path``) — the verified way to make cf-terraforming
cooperate with OpenTofu. ``run`` is injectable for tests.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Optional

RunFn = Callable[..., subprocess.CompletedProcess]

_SCOPE_FLAG = {"zone": "-z", "account": "-a"}


class CfTerraformingError(RuntimeError):
    """A cf-terraforming subprocess exited non-zero."""

    def __init__(self, message: str, *, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def _base_argv(
    subcommand: str,
    *,
    binary: str,
    resource_type: str,
    scope: str,
    scope_id: str,
    install_path: Path,
    tofu_binary: str,
    resource_ids: Optional[list[str]] = None,
) -> list[str]:
    try:
        scope_flag = _SCOPE_FLAG[scope]
    except KeyError as exc:
        raise ValueError(f"scope must be zone|account, got {scope!r}") from exc
    argv = [
        binary, subcommand,
        "--resource-type", resource_type,
        scope_flag, scope_id,
        "--terraform-binary-path", tofu_binary,
        "--terraform-install-path", str(install_path),
    ]
    # Types that cannot be swept (e.g. cloudflare_zone_setting) need their ids
    # named: --resource-id <type>=<id1>,<id2>,...
    if resource_ids:
        argv += ["--resource-id", f"{resource_type}={','.join(resource_ids)}"]
    return argv


def _decode(raw: object) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", "replace")
    return str(raw or "")


def generate(
    *,
    binary: str,
    resource_type: str,
    scope: str,
    scope_id: str,
    install_path: Path,
    tofu_binary: str,
    env: Mapping[str, str],
    resource_ids: Optional[list[str]] = None,
    timeout: int = 900,
    run: RunFn = subprocess.run,
) -> str:
    """Return generated HCL for ``resource_type``. Raises on non-zero exit."""
    argv = _base_argv("generate", binary=binary, resource_type=resource_type, scope=scope,
                      scope_id=scope_id, install_path=install_path, tofu_binary=tofu_binary,
                      resource_ids=resource_ids)
    result = run(argv, env=dict(env), capture_output=True, timeout=timeout)
    if result.returncode != 0:
        stderr = _decode(result.stderr).strip()
        raise CfTerraformingError(
            f"cf-terraforming generate {resource_type} ({scope}={scope_id}) failed: "
            f"{stderr[:1200]}",
            stderr=stderr,
        )
    return _decode(result.stdout)


def import_blocks(
    *,
    binary: str,
    resource_type: str,
    scope: str,
    scope_id: str,
    install_path: Path,
    tofu_binary: str,
    env: Mapping[str, str],
    resource_ids: Optional[list[str]] = None,
    modern_import_block: bool = True,
    timeout: int = 900,
    run: RunFn = subprocess.run,
) -> str:
    """Return ``import{}`` blocks (or terraform-import commands) for a type."""
    argv = _base_argv("import", binary=binary, resource_type=resource_type, scope=scope,
                      scope_id=scope_id, install_path=install_path, tofu_binary=tofu_binary,
                      resource_ids=resource_ids)
    if modern_import_block:
        argv.append("--modern-import-block")
    result = run(argv, env=dict(env), capture_output=True, timeout=timeout)
    if result.returncode != 0:
        stderr = _decode(result.stderr).strip()
        raise CfTerraformingError(
            f"cf-terraforming import {resource_type} ({scope}={scope_id}) failed: {stderr[:1200]}",
            stderr=stderr,
        )
    return _decode(result.stdout)


def version(binary: str = "cf-terraforming", *, run: RunFn = subprocess.run) -> str:
    try:
        result = run([binary, "version"], capture_output=True, timeout=60)
    except Exception:  # noqa: BLE001 - metadata only
        return "unknown"
    text = _decode(result.stdout) or _decode(result.stderr)
    return text.splitlines()[0].strip() if text.strip() else "unknown"


# Expected, non-actionable cf-terraforming outcomes for a blind resource sweep —
# recorded as skips (info), not errors. A 4xx on a list GET means "this type is
# not listable this way"; real errors (5xx / 429 / unexpected) stay surfaced so
# a genuine infra problem stands out in a 257-type sweep. (pattern, human reason)
_BENIGN_PATTERNS: tuple[tuple[str, str], ...] = (
    ("no resource ids defined", "nothing to export (empty, or type needs explicit --resource-id)"),
    ("found to generate", "nothing to export (empty)"),
    ("no route for that uri", "endpoint not available on this account"),
    ("403", "not entitled / insufficient token permission"),
    ("forbidden", "not entitled / insufficient token permission"),
    ("401", "not entitled / insufficient token permission"),
    ("unauthorized", "not entitled / insufficient token permission"),
    ("404", "resource endpoint not present on this account"),
    ("not found", "resource endpoint not present on this account"),
    ("400 bad request", "not listable via a blind sweep (child resource needs a parent id)"),
)


def benign_skip_reason(stderr: str) -> Optional[str]:
    """A reason string if the failure is an expected sweep outcome (empty type,
    not entitled, needs explicit ids/parent id), else None (a real error — 5xx,
    429, unexpected — worth surfacing)."""
    s = (stderr or "").lower()
    # Page Rules are legacy (superseded by rulesets, exported separately).
    if "pagerules" in s and "400" in s:
        return "page rules API returned 400 (legacy feature — superseded by rulesets)"
    # cf-terraforming left a literal {id} placeholder in the URL — it could not
    # resolve a parent id for a child resource (e.g. r2 bucket / magic site).
    if "%7b" in s or "{identifier}" in s:
        return "child resource needs a parent id (cf-terraforming could not resolve it)"
    for pattern, reason in _BENIGN_PATTERNS:
        if pattern in s:
            return reason
    return None


def has_content(hcl: str) -> bool:
    """True if generated HCL actually declares something (not just blanks/comments)."""
    for line in hcl.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("//"):
            return True
    return False


# Shapes of cf-terraforming's (hclwrite-formatted) output.
_RESOURCE_BLOCK = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"\s*\{', re.M)
_IMPORT_BLOCK = re.compile(
    r'import\s*\{\s*to\s*=\s*([A-Za-z0-9_]+)\.([A-Za-z0-9_-]+)\s+'
    r'id\s*=\s*"((?:[^"\\]|\\.)*)"\s*\}')
# cf-terraforming names every resource terraform_managed_resource_<id>_<index>.
_GENERATED_NAME = re.compile(r"^terraform_managed_resource_(.+)_(\d+)$")
# A top-level string attribute of a resource block (two-space indent).
_TOP_LEVEL_STRING = re.compile(r'^  ([A-Za-z0-9_]+)\s*=\s*"((?:[^"\\]|\\.)*)"\s*$', re.M)


def import_block(resource_type: str, name: str, import_id: str) -> str:
    """One ``import {}`` block in cf-terraforming's layout (``import_id`` is
    already HCL-escaped)."""
    return f'import {{\n  to = {resource_type}.{name}\n  id = "{import_id}"\n}}\n'


def _resources(hcl: str, resource_type: str) -> list[tuple[str, str]]:
    """``(name, body)`` of every ``resource_type`` block in ``hcl``."""
    matches = list(_RESOURCE_BLOCK.finditer(hcl))
    out = []
    for i, match in enumerate(matches):
        if match.group(1) != resource_type:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(hcl)
        out.append((match.group(2), hcl[match.end():end]))
    return out


def reconcile_imports(resource_type: str, hcl: str, blocks: str) -> tuple[str, int]:
    """Point cf-terraforming's import blocks at the resources ``generate`` wrote.

    ``generate`` and ``import`` number their resources independently
    (``terraform_managed_resource_<id>_<index>``). For ``cloudflare_ruleset``
    they disagree: generate drops the managed rulesets and sorts by phase,
    import lists every ruleset in API order - so indexes shift and managed
    rulesets get import blocks without a resource, which OpenTofu rejects
    ("Configuration for import target does not exist") and the restore of the
    whole scope fails. Each block is therefore re-pointed to the generated
    resource with the same ``<id>``; blocks without one are dropped.

    Returns ``(blocks, dropped)``. Output whose resource names cannot be read
    is returned unchanged.
    """
    names = [name for name, _ in _resources(hcl, resource_type)]
    imports = _IMPORT_BLOCK.findall(blocks)
    if not names or not imports:
        return blocks, 0
    by_id: dict[str, list[str]] = {}
    for name in names:
        match = _GENERATED_NAME.match(name)
        if match:
            by_id.setdefault(match.group(1), []).append(name)

    taken: set[str] = set()
    kept: list[str] = []
    dropped = 0
    for rtype, name, import_id in imports:
        target = None
        if rtype == resource_type:
            if name in names and name not in taken:
                target = name
            else:
                match = _GENERATED_NAME.match(name)
                free = [n for n in by_id.get(match.group(1), []) if n not in taken] if match else []
                target = free[0] if free else None
        if target is None:
            dropped += 1
            continue
        taken.add(target)
        kept.append(import_block(resource_type, target, import_id))
    return "\n".join(kept), dropped


def imports_from_attributes(resource_type: str, hcl: str,
                            attributes: tuple[str, ...]) -> tuple[str, list[str]]:
    """Import blocks whose id is built from each generated resource's own
    top-level attributes, joined by ``/`` (e.g. ``<account_id>/<tunnel_id>``).

    For types whose API object has no ``id`` cf-terraforming substitutes the
    scope id, which yields an import id OpenTofu cannot resolve. Returns
    ``(blocks, names)`` where ``names`` lists resources lacking an attribute
    (left without an import block)."""
    kept: list[str] = []
    missing: list[str] = []
    for name, body in _resources(hcl, resource_type):
        values = dict(_TOP_LEVEL_STRING.findall(body))
        if not all(values.get(attr) for attr in attributes):
            missing.append(name)
            continue
        kept.append(import_block(resource_type, name,
                                 "/".join(values[attr] for attr in attributes)))
    return "\n".join(kept), missing
