# Backup mode

Automated, scheduled export of the Cloudflare account to Terraform HCL,
bundled + integrity-checked + shipped off-site + retained, by the BackupHelper
engine.

## Run

```bash
cp .env.example .env
docker compose build
docker compose up -d cf-backup     # scheduler daemon (cron)
# or one-off:
docker compose run --rm cf-backup --now
```

Snapshots land in the `/data` volume as `<id>.tar.gz` + `<id>.manifest.json`
(UTC id `%Y-%m-%d_%H-%M-%S`), and — when S3 is configured — under the bucket
prefix `cloudflare/`.

## What gets exported

The `cloudflare` source enumerates **zones** via the Cloudflare API and runs
`cf-terraforming generate` per resource type and scope:

- `resource_scope` = `all` (default) | `zone` | `account`
- `resource_discovery`:
  - `curated` (default) — a maintained allow-list of common zone/account types;
  - `schema` — every resource the pinned provider exposes
    (`tofu providers schema`), classified into zone/account scope.
- Every candidate is validated against the live provider schema; unknown types
  are **skipped with a warning** (see `EXPORT_MANIFEST.json` → `skipped_unknown`).
- A resource type whose generate fails is recorded and skipped — the run
  produces a **partial snapshot**, it does not abort.

Override per deployment in the source config:
`resource_types`, `account_resource_types`, `deny_types`.

**Zone settings** (`cloudflare_zone_setting`) can't be swept — cf-terraforming
needs each setting named. The source exports a curated default set of common,
plan-agnostic settings (SSL/TLS, HTTPS, caching, security level, …) via
`--resource-id`. Customize per type with `resource_ids` in the source config:

```json
{ "type": "cloudflare",
  "resource_ids": { "cloudflare_zone_setting": ["ssl", "min_tls_version", "brotli", "http3"] } }
```

## Configuration

Config is inline `BACKUP_CONFIG_JSON` in `docker-compose.yml`, fed from `.env`.
The `cloudflare` source keys:

| Key | Default | Meaning |
| --- | --- | --- |
| `account_id` | (from zones) | pin to one account |
| `zones` | `auto` | `auto` = all zones the token sees, or a list |
| `resource_scope` | `all` | `all` \| `zone` \| `account` |
| `resource_discovery` | `curated` | `curated` \| `schema` |
| `resource_types` / `account_resource_types` / `deny_types` | — | overrides |
| `throttle_rps` | `4` | request/sec ceiling (global limit 1200 / 5 min) |
| `provider_version` | `>= 5.8.2, < 6.0.0` | provider pin |
| `modern_import_block` | `true` | emit `import{}` blocks |

The API token comes from `CLOUDFLARE_API_TOKEN` in the container env (never the
config). Use a **read-only** token here (Zone/DNS/Account/Workers/Access *Read*).

## Retention (30 days + GFS)

```jsonc
retention: { age_days: 30, gfs: { daily: 7, weekly: 4, monthly: 6 } }
```

- `age_days: 30` prunes anything older than 30 days,
- GFS tiers keep 7 daily / 4 weekly / 6 monthly beyond that,
- `smart_last` (engine default) never prunes the single newest snapshot.

Retention runs after every backup, **independently on local and S3**. Tune via
`BACKUP_RETENTION_AGE_DAYS`, `BACKUP_GFS_DAILY/WEEKLY/MONTHLY`.

## Off-site S3 + encryption

Set `BACKUP_S3_*` (S3-compatible: AWS, MinIO, R2, B2, Wasabi — keep
`force_path_style=true`). Empty bucket ⇒ local-only.

The engine has **no S3 server-side encryption**; for sensitive backups enable
client-side encryption: `BACKUP_ENCRYPTION_MODE=age` +
`BACKUP_ENCRYPTION_RECIPIENT=<age public key>`. Only the ciphertext
(`<id>.tar.gz.age`) is stored/uploaded.

## Scheduling

`BACKUP_SCHEDULE_CRON` (default `15 3 * * *`). The container runs a blocking
scheduler; an external scheduler (GitHub Actions / host cron) can instead invoke
`docker compose run --rm cf-backup --now`.

## Rate limits

Cloudflare's global limit is **1,200 requests / 5 minutes per user, cumulative**.
Keep `throttle_rps ≤ 4`, use a **dedicated backup service-user/token**, and note
that a large account with `resource_discovery: schema` issues many calls — prefer
`curated` unless you need exhaustive coverage.

## Verifying a backup

```bash
docker compose run --rm cf-backup verify <id>   # sha256 vs manifest
docker compose run --rm cf-backup show <id>     # manifest (counts, versions)
```
