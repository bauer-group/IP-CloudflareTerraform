"""The ``cloudflare`` operator command group (backuphelper.commands).

Mounted under the engine CLI as ``backuphelper cloudflare <verb>``:

* ``diff <a> <b>``   — offline, normalized diff of two snapshots' HCL trees
* ``apply <id>``     — restore: push a snapshot's HCL back to Cloudflare (plan-gated)
* ``drift``          — export now and diff against the newest stored snapshot
* ``export``         — ad-hoc export to a local directory (no snapshot)
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

import typer

from . import diff as diff_mod
from . import export as export_mod
from .apply import ApplyError, ApplyResult, apply_export
from .config import CloudflareConfig
from .snapshot import SnapshotError, latest_snapshot_id, open_export

app = typer.Typer(name="cloudflare", help="Cloudflare Terraform backup: diff / apply / drift / export.")


def _load_cfg() -> CloudflareConfig:
    """Build the Cloudflare config from the first job's ``cloudflare`` source so
    provider pins and binary paths match the backup runs. Falls back to defaults
    (token from env) when no such source is configured."""
    from backuphelper.config.loader import load_config

    for job in load_config().jobs:
        for spec in job.sources:
            if spec.type == "cloudflare":
                return CloudflareConfig.from_spec(spec.model_dump())
    return CloudflareConfig()


@app.command("diff")
def diff_cmd(
    snapshot_a: str = typer.Argument(..., help="baseline snapshot id"),
    snapshot_b: str = typer.Argument(..., help="comparison snapshot id"),
    raw: bool = typer.Option(False, "--raw", help="verbatim diff (no whitespace normalization)"),
    exit_code: bool = typer.Option(False, "--exit-code", "-x", help="exit 1 when snapshots differ"),
) -> None:
    """Compare two backups offline (no API calls)."""
    try:
        with open_export(snapshot_a) as tree_a, open_export(snapshot_b) as tree_b:
            result = diff_mod.diff_trees(tree_a, tree_b, raw=raw,
                                         label_a=snapshot_a, label_b=snapshot_b)
    except SnapshotError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2)

    if result.version_warning:
        typer.echo(f"⚠️  {result.version_warning}", err=True)
    if not result.has_changes:
        typer.echo(f"no differences between {snapshot_a} and {snapshot_b}")
        raise typer.Exit(0)

    typer.echo(f"changed: {len(result.changed)}  added: {len(result.added)}  "
               f"removed: {len(result.removed)}")
    typer.echo("")
    typer.echo(result.text)
    raise typer.Exit(1 if exit_code else 0)


@app.command("apply")
def apply_cmd(
    snapshot_id: str = typer.Argument(..., help="snapshot id to restore from"),
    zone: Optional[str] = typer.Option(None, "--zone", help="zone name to apply (single scope)"),
    account: Optional[str] = typer.Option(None, "--account", help="account id to apply (single scope)"),
    dr: bool = typer.Option(False, "--dr", help="disaster recovery: create from scratch (skip import blocks)"),
    force: bool = typer.Option(False, "--force", "-f", help="apply without interactive approval (unattended reconcile)"),
    plan_only: bool = typer.Option(False, "--plan-only", help="show the plan and stop (never apply)"),
) -> None:
    """Restore: apply a snapshot's HCL back to Cloudflare. Plan-gated by default."""
    cfg = _load_cfg()
    zone_slug = export_mod.slug(zone) if zone else None
    account_slug = export_mod.slug(account) if account else None

    def _confirm(result: ApplyResult) -> bool:
        typer.echo(result.plan_text)
        if result.secret_warnings:
            typer.echo("")
            typer.echo("⚠️  Secrets that do NOT round-trip — re-inject before/after apply:")
            for w in result.secret_warnings:
                typer.echo(f"   - {w['resource_type']}: {w['lost_value']}")
        typer.echo("")
        return typer.confirm(
            f"Apply this plan to Cloudflare scope '{result.scope}'? This CHANGES live config.")

    try:
        with open_export(snapshot_id) as tree:
            result = apply_export(
                tree, cfg, zone_slug=zone_slug, account_slug=account_slug, dr=dr,
                force=force, plan_only=plan_only, confirm=_confirm)
    except (SnapshotError, ApplyError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2)

    if plan_only:
        typer.echo(result.plan_text)
        if result.secret_warnings:
            typer.echo("\n⚠️  Secrets to re-inject before a real apply:")
            for w in result.secret_warnings:
                typer.echo(f"   - {w['resource_type']}: {w['lost_value']}")
        raise typer.Exit(0)

    if not result.applied:
        typer.echo("aborted — nothing applied")
        raise typer.Exit(0)
    typer.echo(f"applied scope '{result.scope}'. Re-plan (should be empty):")
    typer.echo(result.replan_text)
    raise typer.Exit(0)


@app.command("drift")
def drift_cmd(
    zone: Optional[str] = typer.Option(None, "--zone", help="restrict to a single zone"),
    raw: bool = typer.Option(False, "--raw", help="verbatim diff"),
    against: Optional[str] = typer.Option(None, "--against", help="baseline snapshot id (default: newest)"),
) -> None:
    """Export now and diff against the newest stored snapshot (or --against)."""
    baseline = against or latest_snapshot_id()
    if baseline is None:
        typer.echo("error: no stored snapshot to compare against", err=True)
        raise typer.Exit(2)

    cfg = _load_cfg()
    if zone:
        cfg.zones = [zone]
    with tempfile.TemporaryDirectory(prefix="cf-drift-") as td:
        fresh = Path(td) / "now"
        result_export = export_mod.export(cfg, fresh)
        if result_export.files_written == 0:
            typer.echo("error: fresh export produced no files "
                       f"({'; '.join(result_export.errors[:2]) or 'unknown'})", err=True)
            raise typer.Exit(2)
        try:
            with open_export(baseline) as tree_old:
                result = diff_mod.diff_trees(tree_old, fresh, raw=raw,
                                             label_a=baseline, label_b="now")
        except SnapshotError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2)

    if result.version_warning:
        typer.echo(f"⚠️  {result.version_warning}", err=True)
    if not result.has_changes:
        typer.echo(f"no drift since {baseline}")
        raise typer.Exit(0)
    typer.echo(f"DRIFT since {baseline} — changed: {len(result.changed)}  "
               f"added: {len(result.added)}  removed: {len(result.removed)}")
    typer.echo("")
    typer.echo(result.text)
    raise typer.Exit(1)


@app.command("export")
def export_cmd(
    out: Path = typer.Option(..., "--out", help="output directory for the HCL tree"),
) -> None:
    """Ad-hoc export of the live account to a local HCL tree (no snapshot)."""
    cfg = _load_cfg()
    result = export_mod.export(cfg, out)
    typer.echo(f"exported {result.files_written} file(s) from "
               f"{result.zone_count} zone(s) → {out}")
    if result.skipped_unknown:
        typer.echo(f"skipped {len(set(result.skipped_unknown))} unknown type(s): "
                   f"{sorted(set(result.skipped_unknown))}")
    if result.skipped:
        typer.echo(f"skipped {len(result.skipped)} type(s) (empty / not entitled / need ids)")
    if result.secrets_report:
        typer.echo("secrets to re-inject on restore:")
        for w in result.secrets_report:
            typer.echo(f"   - {w['resource_type']}: {w['lost_value']}")
    if result.errors:
        typer.echo(f"{len(result.errors)} warning(s); see {out}/{export_mod.EXPORT_MANIFEST_NAME}")
    raise typer.Exit(0)
