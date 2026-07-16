"""Shared test doubles: fake subprocess runners and a fake Cloudflare API."""

from __future__ import annotations

import json
import subprocess
from typing import Callable, Optional


def proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def make_tofu_run(resource_types: Optional[set[str]] = None,
                  version: str = "OpenTofu v1.11.0") -> Callable:
    """A fake ``tofu`` runner: init/fmt/apply ok, providers-schema returns the
    given resource types, version returns a fixed string."""
    schema = {
        "provider_schemas": {
            "registry.terraform.io/cloudflare/cloudflare": {
                "resource_schemas": {t: {} for t in (resource_types or set())}
            }
        }
    }

    def run(argv, **kwargs):
        if len(argv) >= 3 and argv[1] == "providers" and argv[2] == "schema":
            return proc(stdout=json.dumps(schema).encode())
        if len(argv) >= 2 and argv[1] == "version":
            return proc(stdout=version.encode())
        # init / fmt / plan / apply
        return proc(stdout=b"ok")

    return run


def make_cf_run(hcl_by_type: dict[str, str], *,
                fail_types: Optional[set[str]] = None,
                benign_types: Optional[set[str]] = None,
                version: str = "cf-terraforming v0.27.0",
                record: Optional[list] = None) -> Callable:
    """A fake ``cf-terraforming`` runner.

    ``generate <type>`` returns ``hcl_by_type[type]`` (empty string if absent);
    types in ``fail_types`` exit non-zero with a real error; types in
    ``benign_types`` exit non-zero with an expected ("No resource IDs defined")
    stderr. ``import`` returns a stub import block. ``record`` (if given)
    collects every argv for assertions.
    """
    fail_types = fail_types or set()
    benign_types = benign_types or set()

    def run(argv, **kwargs):
        if record is not None:
            record.append(list(argv))
        if len(argv) >= 2 and argv[1] == "version":
            return proc(stdout=version.encode())
        # argv = [bin, sub, "--resource-type", TYPE, scope_flag, id, ...]
        sub = argv[1]
        rtype = argv[argv.index("--resource-type") + 1]
        if rtype in benign_types:
            return proc(returncode=1,
                        stderr=f'level=fatal msg="No resource IDs defined in Terraform for '
                               f'resource {rtype}"'.encode())
        if rtype in fail_types:
            return proc(returncode=1, stderr=f"boom {rtype}".encode())
        if sub == "generate":
            return proc(stdout=hcl_by_type.get(rtype, "").encode())
        if sub == "import":
            body = f'import {{\n  to = {rtype}.x\n  id = "abc"\n}}\n'
            return proc(stdout=body.encode() if rtype in hcl_by_type else b"")
        return proc()

    return run


def make_fetch(pages: list[dict]) -> Callable:
    """A fake Cloudflare API fetch returning successive ``pages`` per call."""
    seq = list(pages)

    def fetch(url, token, **kwargs):
        return seq.pop(0) if seq else {"success": True, "result": [], "result_info": {"total_pages": 1}}

    return fetch


def zone_page(zones: list[tuple[str, str, str]], *, page: int = 1, total_pages: int = 1) -> dict:
    """Build a /zones API page from ``(id, name, account_id)`` tuples."""
    return {
        "success": True,
        "result": [{"id": zid, "name": name, "account": {"id": acct}} for zid, name, acct in zones],
        "result_info": {"page": page, "total_pages": total_pages},
    }
