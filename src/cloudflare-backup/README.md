# cloudflare-backup

Cloudflare configuration backup / restore / diff, built on the central
**BackupHelper engine** (`ghcr.io/bauer-group/cs-backuphelper/backuphelper`).

This image adds the `tofu` + `cf-terraforming` binaries and a pip plugin that
registers, in one dual-mode image:

- a **`cloudflare` backup source** — exports the live account to OpenTofu HCL via
  `cf-terraforming`; the engine then hashes it, bundles it deterministically,
  optionally encrypts (age/gpg), uploads off-site to S3, applies retention and
  alerts;
- a **`cloudflare` command group** — the Cloudflare/Terraform-specific verbs the
  generic engine lacks: `diff`, `apply`, `drift`, `export`.

All backup mechanics (sha256 manifest, deterministic `tar.gz`, S3, retention,
notifications, the restore CLI) live in the engine — see its docs.

## Modes

```bash
# Backup mode — scheduler daemon (cron → S3, retention 30d + GFS):
docker compose up -d cf-backup

# Ad-hoc backup + engine management (CLI):
docker compose run --rm cf-backup --now
docker compose run --rm cf-backup list
docker compose run --rm cf-backup verify <id>
docker compose run --rm cf-backup prune
docker compose run --rm cf-backup config --show-secrets  # redacted by default

# Cloudflare verbs (this plugin):
docker compose run --rm cf-backup cloudflare diff <id-a> <id-b>
docker compose run --rm cf-backup cloudflare drift
docker compose run --rm cf-backup cloudflare apply <id> --zone example.com
docker compose run --rm cf-backup cloudflare export --out /data/adhoc
```

## Configuration

The compose service passes the whole job config inline as `BACKUP_CONFIG_JSON`
(a `cloudflare` source, `local` + off-site `s3` destinations, cron schedule,
`age_days`+GFS retention, notifications). The API token is supplied via the
`CLOUDFLARE_API_TOKEN` env var (never written into the rendered config); the
`cloudflare` source reads it from the container environment.

Key `cloudflare` source keys: `account_id`, `zones` (`"auto"` or a list),
`resource_scope` (`all|zone|account`), `resource_discovery` (`curated|schema`),
`resource_types` / `account_resource_types` / `deny_types` overrides,
`throttle_rps` (default 4, respecting the 1200 req / 5 min limit),
`provider_version`, `modern_import_block`.

## Restore

`cloudflare apply <id>` pushes a snapshot's HCL back to Cloudflare, **plan-gated**
(plan → human approval → apply → re-plan). One scope per run (`--zone` or
`--account`). `--dr` recreates from scratch (no import blocks); `--force` runs
unattended. Resources whose secret payload does not round-trip (Access service
tokens, tunnels, certificates, API tokens, Worker secrets) are reported and must
be re-injected — the engine's `restore <id>` only rehydrates the HCL files, it
does not push to Cloudflare.

> **Not covered:** data-plane content (KV values, R2 objects, D1 rows) is not
> configuration and is out of scope.
