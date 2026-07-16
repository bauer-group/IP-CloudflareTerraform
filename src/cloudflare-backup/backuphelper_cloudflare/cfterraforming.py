"""Thin wrapper around the ``cf-terraforming`` binary.

``generate`` emits Terraform HCL for a resource type; ``import_blocks`` emits the
``import{}`` blocks (or ``terraform import`` commands) that bind live resources
into state. Both drive OpenTofu via ``--terraform-binary-path`` and read the
provider schema from an already-initialized working dir
(``--terraform-install-path``) — the verified way to make cf-terraforming
cooperate with OpenTofu. ``run`` is injectable for tests.
"""

from __future__ import annotations

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
# recorded as skips (info), not errors. (pattern, human reason)
_BENIGN_PATTERNS: tuple[tuple[str, str], ...] = (
    ("no resource ids defined", "nothing to export (empty, or type needs explicit --resource-id)"),
    ("found to generate", "nothing to export (empty)"),
    ("403", "not entitled / insufficient token permission"),
    ("forbidden", "not entitled / insufficient token permission"),
    ("404", "resource endpoint not present on this account"),
    ("not found", "resource endpoint not present on this account"),
)


def benign_skip_reason(stderr: str) -> Optional[str]:
    """A reason string if the failure is an expected sweep outcome (empty type,
    not entitled, needs explicit ids), else None (a real error worth surfacing)."""
    s = (stderr or "").lower()
    # Page Rules are a legacy feature superseded by rulesets (which are exported
    # separately); the /pagerules endpoint returns 400 on zones without/beyond
    # it. Specific to that endpoint so unrelated 400s stay real errors.
    if "pagerules" in s and "400" in s:
        return "page rules API returned 400 (legacy feature — superseded by rulesets)"
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
