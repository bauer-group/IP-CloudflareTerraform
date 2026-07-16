"""Typed configuration for the Cloudflare source and command verbs.

Uses a plain dataclass (no pydantic) so the whole core stays dependency-free and
unit-testable on any host without the engine installed. The engine hands the
source an open ``spec`` dict (``SourceSpec`` is ``extra="allow"``); ``from_spec``
extracts the known keys, applies defaults and coerces types.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Union

# Provider/tooling pins — aligned with the existing IAMStack
# infrastructure/cloudflare module (validated against provider v5.21.1).
DEFAULT_PROVIDER_VERSION = ">= 5.8.2, < 6.0.0"
# Provider v5 uses write-only attributes that require Terraform/OpenTofu >= 1.11.
DEFAULT_REQUIRED_VERSION = ">= 1.11"
DEFAULT_API_BASE = "https://api.cloudflare.com/client/v4"


class ConfigError(ValueError):
    """A cloudflare source/command was configured with invalid values."""


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_list(value: Any) -> list[str]:
    """Accept a list or a comma-separated string; drop blanks."""
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return [str(p).strip() for p in value if str(p).strip()]


@dataclass
class CloudflareConfig:
    """Resolved configuration for one ``cloudflare`` source / command run."""

    name: str = "cloudflare"
    # Auth: token is read from the container env by default (never logged / never
    # on a command line). An explicit spec value overrides, but env is preferred.
    api_token: Optional[str] = None
    account_id: Optional[str] = None

    # Scope. ``zones`` is "auto" (discover every zone in the account) or an
    # explicit list of zone names or ids.
    zones: Union[str, list[str]] = "auto"
    resource_scope: str = "all"  # all | zone | account

    # Resource-type selection. ``curated`` uses the built-in allow-lists;
    # ``schema`` enumerates every resource the pinned provider exposes (via
    # ``tofu providers schema``) and classifies scope by ACCOUNT_SCOPED_TYPES.
    resource_discovery: str = "curated"
    resource_types: list[str] = field(default_factory=list)          # zone-level override
    account_resource_types: list[str] = field(default_factory=list)  # account-level override
    deny_types: list[str] = field(default_factory=list)              # added to the built-in deny-list
    # Per-type explicit ids for types that cannot be swept (cf-terraforming
    # --resource-id), e.g. {"cloudflare_zone_setting": ["ssl", "brotli"]}.
    # Empty for a type → the built-in RESOURCE_ID_DEFAULTS apply.
    resource_ids: dict = field(default_factory=dict)

    # Tooling.
    provider_version: str = DEFAULT_PROVIDER_VERSION
    required_version: str = DEFAULT_REQUIRED_VERSION
    tofu_binary: str = "tofu"
    cfterraforming_binary: str = "cf-terraforming"
    api_base: str = DEFAULT_API_BASE

    # Behaviour.
    throttle_rps: float = 4.0          # Cloudflare global limit is 1200 req / 5 min
    timeout: int = 900                 # per cf-terraforming / tofu subprocess call, seconds
    modern_import_block: bool = True   # emit import{} blocks (Terraform/OpenTofu >= 1.5)
    validate: bool = False             # run `tofu validate` (some v5 HCL legitimately fails it)

    def tofu_binary_path(self) -> str:
        """Absolute path to the tofu binary for cf-terraforming's
        ``--terraform-binary-path``. cf-terraforming ``stat``s the literal value
        (it does NOT search PATH), so a bare name like ``tofu`` must be resolved
        to a full path first."""
        import shutil

        if os.path.isabs(self.tofu_binary):
            return self.tofu_binary
        return shutil.which(self.tofu_binary) or self.tofu_binary

    def resolve_token(self, env: Optional[Mapping[str, str]] = None) -> str:
        """The API token, preferring the container env over the spec."""
        env = os.environ if env is None else env
        token = env.get("CLOUDFLARE_API_TOKEN") or self.api_token
        if not token:
            raise ConfigError(
                "no Cloudflare API token: set CLOUDFLARE_API_TOKEN in the environment "
                "(preferred) or api_token in the source config"
            )
        return token

    @property
    def zone_selection(self) -> Optional[list[str]]:
        """Explicit zone name/id list, or None when auto-discovering."""
        if isinstance(self.zones, str):
            return None if self.zones.strip().lower() == "auto" else _as_list(self.zones)
        return _as_list(self.zones)

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> "CloudflareConfig":
        s = {k: v for k, v in spec.items() if k not in {"type", "enabled"}}
        scope = str(s.get("resource_scope", "all")).lower()
        if scope not in {"all", "zone", "account"}:
            raise ConfigError(f"resource_scope must be all|zone|account, got {scope!r}")
        discovery = str(s.get("resource_discovery", "curated")).lower()
        if discovery not in {"curated", "schema"}:
            raise ConfigError(f"resource_discovery must be curated|schema, got {discovery!r}")

        zones = s.get("zones", "auto")
        if isinstance(zones, str) and zones.strip().lower() != "auto":
            zones = _as_list(zones)

        raw_ids = s.get("resource_ids") or {}
        resource_ids = ({str(k): _as_list(v) for k, v in raw_ids.items()}
                        if isinstance(raw_ids, dict) else {})

        try:
            throttle = float(s.get("throttle_rps", 4.0))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"throttle_rps must be a number: {exc}") from exc
        if throttle <= 0:
            raise ConfigError("throttle_rps must be > 0")

        return cls(
            name=str(s.get("name", "cloudflare")),
            api_token=s.get("api_token"),
            account_id=s.get("account_id") or None,
            zones=zones,
            resource_scope=scope,
            resource_discovery=discovery,
            resource_types=_as_list(s.get("resource_types")),
            account_resource_types=_as_list(s.get("account_resource_types")),
            deny_types=_as_list(s.get("deny_types")),
            resource_ids=resource_ids,
            provider_version=str(s.get("provider_version", DEFAULT_PROVIDER_VERSION)),
            required_version=str(s.get("required_version", DEFAULT_REQUIRED_VERSION)),
            tofu_binary=str(s.get("tofu_binary", "tofu")),
            cfterraforming_binary=str(s.get("cfterraforming_binary", "cf-terraforming")),
            api_base=str(s.get("api_base", DEFAULT_API_BASE)).rstrip("/"),
            throttle_rps=throttle,
            timeout=int(s.get("timeout", 900)),
            modern_import_block=_as_bool(s.get("modern_import_block"), True),
            validate=_as_bool(s.get("validate"), False),
        )
