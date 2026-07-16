"""Offline, normalized diff between two exported HCL trees.

Both snapshots were already ``tofu fmt``-canonicalized at backup time, so a plain
per-file unified diff over the ``.tf`` tree is deterministic and order-stable.
``raw=False`` additionally ignores pure-whitespace churn; ``raw=True`` compares
verbatim. Pure ``difflib`` — no git, no network — so it unit-tests trivially.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .export import EXPORT_MANIFEST_NAME


@dataclass
class DiffResult:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    text: str = ""
    version_warning: Optional[str] = None

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed)


def _tf_files(root: Path) -> dict[str, str]:
    root = Path(root)
    files: dict[str, str] = {}
    for path in root.rglob("*.tf"):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            files[rel] = path.read_text(encoding="utf-8", errors="replace")
    return files


def _normalize(text: str) -> list[str]:
    """Strip trailing whitespace, collapse runs of blank lines, and drop
    leading/trailing blank lines so pure-whitespace churn does not diff."""
    out: list[str] = []
    blank = False
    for line in text.splitlines():
        stripped = line.rstrip()
        if not stripped:
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(stripped)
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return out


def diff_trees(tree_a: Path, tree_b: Path, *, raw: bool = False,
               label_a: str = "a", label_b: str = "b") -> DiffResult:
    """Diff every ``.tf`` file under two export trees."""
    files_a = _tf_files(tree_a)
    files_b = _tf_files(tree_b)
    keys_a, keys_b = set(files_a), set(files_b)

    result = DiffResult(
        added=sorted(keys_b - keys_a),
        removed=sorted(keys_a - keys_b),
    )

    chunks: list[str] = []
    for rel in sorted(keys_a & keys_b):
        if raw:
            a_lines = files_a[rel].splitlines()
            b_lines = files_b[rel].splitlines()
        else:
            a_lines = _normalize(files_a[rel])
            b_lines = _normalize(files_b[rel])
        if a_lines == b_lines:
            continue
        result.changed.append(rel)
        chunks.append("\n".join(difflib.unified_diff(
            a_lines, b_lines,
            fromfile=f"{label_a}/{rel}", tofile=f"{label_b}/{rel}", lineterm="")))

    for rel in result.removed:
        chunks.append(f"--- {label_a}/{rel}\n+++ {label_b}/{rel}\n(removed)")
    for rel in result.added:
        chunks.append(f"--- {label_a}/{rel}\n+++ {label_b}/{rel}\n(added)")

    result.text = "\n".join(chunks)
    result.version_warning = _version_gate(tree_a, tree_b)
    return result


def read_export_manifest(tree: Path) -> dict:
    path = Path(tree) / EXPORT_MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def _version_gate(tree_a: Path, tree_b: Path) -> Optional[str]:
    """Warn when the two snapshots were produced by different tool/provider
    versions — schema churn makes cross-version diffs unreliable."""
    va = read_export_manifest(tree_a).get("tool_versions", {})
    vb = read_export_manifest(tree_b).get("tool_versions", {})
    if not va or not vb:
        return None
    mismatched = [k for k in ("provider_version", "cf_terraforming_version", "tofu_version")
                  if va.get(k) != vb.get(k)]
    if not mismatched:
        return None
    detail = ", ".join(f"{k}: {va.get(k)!r} vs {vb.get(k)!r}" for k in mismatched)
    return f"tool/provider versions differ between snapshots ({detail}); diff may be noisy"
