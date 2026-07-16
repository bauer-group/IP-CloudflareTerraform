"""Thin wrapper around the OpenTofu (``tofu``) binary.

Only what the source/apply flows need: render a pinned provider config, init a
working dir (so cf-terraforming can read the provider schema), read that schema,
canonicalize with ``fmt``, and plan/apply for restore. ``run`` is injectable so
tests never shell out.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Optional

RunFn = Callable[..., subprocess.CompletedProcess]


class TofuError(RuntimeError):
    """A tofu subprocess exited non-zero."""


def provider_config(provider_version: str, required_version: str) -> str:
    """The ``main.tf`` that pins the Cloudflare provider. The token is supplied
    via the CLOUDFLARE_API_TOKEN env var, never written into config."""
    return (
        "terraform {\n"
        f'  required_version = "{required_version}"\n'
        "  required_providers {\n"
        "    cloudflare = {\n"
        '      source  = "cloudflare/cloudflare"\n'
        f'      version = "{provider_version}"\n'
        "    }\n"
        "  }\n"
        "}\n\n"
        'provider "cloudflare" {}\n'
    )


def write_provider_config(workdir: Path, provider_version: str, required_version: str) -> Path:
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    main_tf = workdir / "main.tf"
    main_tf.write_text(provider_config(provider_version, required_version), encoding="utf-8")
    return main_tf


def _run(
    argv: list[str],
    *,
    workdir: Path,
    env: Optional[Mapping[str, str]],
    timeout: int,
    run: RunFn,
) -> subprocess.CompletedProcess:
    return run(
        argv,
        cwd=str(workdir),
        env=dict(env) if env is not None else None,
        capture_output=True,
        timeout=timeout,
    )


def _check(result: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    if result.returncode != 0:
        msg = (result.stderr or b"")
        text = msg.decode("utf-8", "replace") if isinstance(msg, bytes) else str(msg)
        raise TofuError(f"{what} failed (exit {result.returncode}): {text.strip()[:800]}")
    return result


def init(workdir: Path, *, binary: str = "tofu", env: Optional[Mapping[str, str]] = None,
         timeout: int = 900, run: RunFn = subprocess.run) -> None:
    _check(_run([binary, "init", "-input=false", "-no-color"],
               workdir=workdir, env=env, timeout=timeout, run=run), "tofu init")


def providers_schema(workdir: Path, *, binary: str = "tofu",
                     env: Optional[Mapping[str, str]] = None, timeout: int = 300,
                     run: RunFn = subprocess.run) -> dict:
    """Return the parsed ``tofu providers schema -json`` document."""
    result = _check(
        _run([binary, "providers", "schema", "-json"],
             workdir=workdir, env=env, timeout=timeout, run=run),
        "tofu providers schema",
    )
    out = result.stdout
    text = out.decode("utf-8") if isinstance(out, (bytes, bytearray)) else str(out)
    return json.loads(text)


def provider_resource_types(schema: dict) -> set[str]:
    """Extract every managed resource type name from a providers-schema doc."""
    types: set[str] = set()
    for provider in (schema.get("provider_schemas") or {}).values():
        types.update((provider.get("resource_schemas") or {}).keys())
    return types


def fmt(workdir: Path, *, binary: str = "tofu", env: Optional[Mapping[str, str]] = None,
        timeout: int = 120, run: RunFn = subprocess.run) -> None:
    # fmt is best-effort canonicalization; a non-zero exit must not fail a backup.
    _run([binary, "fmt", "-recursive", "-no-color"],
         workdir=workdir, env=env, timeout=timeout, run=run)


def plan(workdir: Path, *, binary: str = "tofu", env: Optional[Mapping[str, str]] = None,
         out: Optional[str] = None, timeout: int = 900,
         run: RunFn = subprocess.run) -> subprocess.CompletedProcess:
    argv = [binary, "plan", "-input=false", "-no-color"]
    if out:
        argv.append(f"-out={out}")
    return _check(_run(argv, workdir=workdir, env=env, timeout=timeout, run=run), "tofu plan")


def apply(workdir: Path, *, binary: str = "tofu", env: Optional[Mapping[str, str]] = None,
          plan_file: Optional[str] = None, timeout: int = 1800,
          run: RunFn = subprocess.run) -> subprocess.CompletedProcess:
    argv = [binary, "apply", "-input=false", "-no-color", "-auto-approve"]
    if plan_file:
        argv.append(plan_file)
    return _check(_run(argv, workdir=workdir, env=env, timeout=timeout, run=run), "tofu apply")


def version(binary: str = "tofu", *, run: RunFn = subprocess.run) -> str:
    try:
        result = run([binary, "version"], capture_output=True, timeout=60)
    except Exception:  # noqa: BLE001 - version is metadata; never fatal
        return "unknown"
    out = result.stdout
    text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out or "")
    return text.splitlines()[0].strip() if text.strip() else "unknown"
