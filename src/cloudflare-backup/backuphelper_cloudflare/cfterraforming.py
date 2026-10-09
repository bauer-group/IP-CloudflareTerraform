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
# The start of any import block, whatever its layout.
_IMPORT_START = re.compile(r"^\s*import\s*\{", re.M)
# cf-terraforming names every resource terraform_managed_resource_<id>_<index>.
_GENERATED_NAME = re.compile(r"^terraform_managed_resource_(.+)_(\d+)$")
# A top-level string attribute of a resource block (two-space indent).
_TOP_LEVEL_STRING = re.compile(r'^  ([A-Za-z0-9_]+)\s*=\s*"((?:[^"\\]|\\.)*)"\s*$', re.M)


def _import_blocks(blocks: str) -> Optional[list[tuple[str, str, str]]]:
    """``(type, name, id)`` of every import block in ``blocks``, or None when
    a block is not in the layout above (e.g. a later cf-terraforming adds an
    attribute). The rewrites below rebuild the file from the blocks they
    read, so they leave such output unchanged rather than lose a block."""
    found = _IMPORT_BLOCK.findall(blocks)
    return found if len(found) == len(_IMPORT_START.findall(blocks)) else None


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

    Returns ``(blocks, dropped)``. Output whose resource names or import
    blocks cannot all be read is returned unchanged.
    """
    names = [name for name, _ in _resources(hcl, resource_type)]
    imports = _import_blocks(blocks)
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


# The API's (camelCase) keys of a tunnel ingress configuration and the
# provider v5 attributes they map to (json/tfsdk tags of the provider's
# zero_trust_tunnel_cloudflared_config model).
TUNNEL_CONFIG_KEYS: dict[str, str] = {
    "originRequest": "origin_request", "audTag": "aud_tag", "caPool": "ca_pool",
    "connectTimeout": "connect_timeout", "disableChunkedEncoding": "disable_chunked_encoding",
    "http2Origin": "http2_origin", "httpHostHeader": "http_host_header",
    "keepAliveConnections": "keep_alive_connections", "keepAliveTimeout": "keep_alive_timeout",
    "matchSNItoHost": "match_sn_ito_host", "noHappyEyeballs": "no_happy_eyeballs",
    "noTLSVerify": "no_tls_verify", "originServerName": "origin_server_name",
    "proxyType": "proxy_type", "tcpKeepAlive": "tcp_keep_alive", "teamName": "team_name",
    "tlsTimeout": "tls_timeout",
}
_TUNNEL_CONFIG_KEYS = re.compile(
    rf"^(\s+)({'|'.join(TUNNEL_CONFIG_KEYS)})(\s*=)", re.M)


def adapt_to_provider(resource_type: str, hcl: str) -> str:
    """Rewrite cf-terraforming output the provider can never import without a
    change in the plan, keeping what a restore applies the same:

    * ``cloudflare_managed_transforms``: the provider imports only the enabled
      transforms and, on apply, disables every enabled transform the config
      leaves out. The disabled ones cf-terraforming lists add nothing but a
      change on every restore - only the enabled ones are kept.
    * ``cloudflare_snippet_rules``: cf-terraforming wraps a zone's snippet
      rules into one resource even when there are none, and the provider
      imports the resource without its rules - an empty one would plan a
      write on every restore of every zone. Resources with no rules are
      dropped, like any other type without resources.
    * ``cloudflare_load_balancer_monitor``: the API returns ``header: {}`` for
      a monitor without headers, which the provider imports as null - the
      empty map is dropped. A header added after the backup still shows up
      in the plan (and is removed by the restore), as the attribute is not
      computed.
    * ``cloudflare_load_balancer_pool``: an origin's ``header`` comes from the
      API as ``{"Host": [...]}``; cf-terraforming writes the key as is, but the
      provider's attribute is ``host``. OpenTofu silently drops the unknown
      key, so a restore would *remove* the origin's Host header - the key is
      renamed to ``host``.
    * ``cloudflare_zero_trust_tunnel_cloudflared_config``: the same for the
      camelCase keys of a tunnel's ``originRequest`` settings
      (``noTLSVerify``, ``httpHostHeader``, ...), which the provider names in
      snake_case (``TUNNEL_CONFIG_KEYS``) - without the rename a restore
      would wipe them.
    """
    if resource_type == "cloudflare_load_balancer_monitor":
        return re.sub(r"^  header\s*=\s*\{\s*\}[ \t]*\n", "", hcl, flags=re.M)
    if resource_type == "cloudflare_load_balancer_pool":
        return re.sub(r"^(\s+)Host(\s*=)", r"\1host\2", hcl, flags=re.M)
    if resource_type == "cloudflare_zero_trust_tunnel_cloudflared_config":
        return _TUNNEL_CONFIG_KEYS.sub(
            lambda m: m.group(1) + TUNNEL_CONFIG_KEYS[m.group(2)] + m.group(3), hcl)
    if resource_type == "cloudflare_managed_transforms":
        for attribute in ("managed_request_headers", "managed_response_headers"):
            hcl = _enabled_only(hcl, attribute)
        return hcl
    if resource_type == "cloudflare_snippet_rules":
        return _without_blocks(hcl, resource_type, re.compile(r"^  rules\s*=\s*\[\s*\]\s*$", re.M))
    return hcl


def _closing(text: str, start: int) -> int:
    """Index of the bracket closing the one at ``start``, -1 if none (strings
    are skipped)."""
    pairs = {"[": "]", "{": "}"}
    stack: list[str] = []
    in_string = escaped = False
    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack:
                return i
    return -1


def _enabled_only(hcl: str, attribute: str) -> str:
    """``attribute = [{enabled, id}, ...]`` reduced to the enabled entries."""
    match = re.search(rf"^(\s*){attribute}\s*=\s*\[", hcl, re.M)
    if not match:
        return hcl
    start = match.end() - 1
    end = _closing(hcl, start)
    if end < 0:
        return hcl
    kept: list[str] = []
    position = start + 1
    while (opening := hcl.find("{", position, end)) >= 0:
        closing = _closing(hcl, opening)
        if closing < 0 or closing > end:
            return hcl
        entry = hcl[opening:closing + 1]
        position = closing + 1
        if re.search(r"\benabled\s*=\s*false\b", entry):
            continue
        id_match = re.search(r'\bid\s*=\s*("(?:[^"\\]|\\.)*")', entry)
        if id_match is None:
            return hcl
        kept.append(f"{{ enabled = true, id = {id_match.group(1)} }}")
    return f"{hcl[:start]}[{', '.join(kept)}]{hcl[end + 1:]}"


def _without_blocks(hcl: str, resource_type: str, empty: re.Pattern) -> str:
    """``hcl`` without the ``resource_type`` blocks whose body matches ``empty``."""
    matches = list(_RESOURCE_BLOCK.finditer(hcl))
    out, position = [], 0
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(hcl)
        if match.group(1) == resource_type and empty.search(hcl[match.end():end]):
            out.append(hcl[position:match.start()])
            position = end
    out.append(hcl[position:])
    return "".join(out)


def imports_from_attributes(resource_type: str, hcl: str,
                            attributes: tuple[str, ...]) -> tuple[str, list[str]]:
    """Import blocks whose id is built from each generated resource's own
    top-level attributes, joined by ``/`` (e.g. ``<account_id>/<tunnel_id>``).
    An attribute given as ``name=default`` falls back to ``default`` when the
    resource does not set it.

    For types whose API object has no ``id`` cf-terraforming substitutes the
    scope id, which yields an import id OpenTofu cannot resolve. Returns
    ``(blocks, names)`` where ``names`` lists resources lacking an attribute
    (left without an import block)."""
    kept: list[str] = []
    missing: list[str] = []
    for name, body in _resources(hcl, resource_type):
        values = dict(_TOP_LEVEL_STRING.findall(body))
        parts = []
        for spec in attributes:
            attribute, _, default = spec.partition("=")
            parts.append(values.get(attribute) or default)
        if not all(parts):
            missing.append(name)
            continue
        kept.append(import_block(resource_type, name, "/".join(parts)))
    return "\n".join(kept), missing


def drop_imports_with_id(blocks: str, import_id: str) -> tuple[str, int]:
    """``blocks`` without the import blocks whose id is ``import_id``.

    cf-terraforming 0.27 uses the scope id as the id of every object its list
    endpoint returns without an ``id`` field (e.g. Turnstile widgets, queues,
    Web Analytics sites), which yields ``<scope id>/<scope id>``: an object
    that does not exist, and an import that fails the whole plan. Output
    whose import blocks cannot all be read is returned unchanged."""
    imports = _import_blocks(blocks)
    if imports is None:
        return blocks, 0
    kept, dropped = [], 0
    for rtype, name, block_id in imports:
        if block_id == import_id:
            dropped += 1
            continue
        kept.append(import_block(rtype, name, block_id))
    if not dropped:
        return blocks, 0
    return "\n".join(kept), dropped
