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
  - **`schema` (default)** — enumerate **every** resource type the pinned
    provider exposes (`tofu providers schema`, ~257) for maximum backup &
    recovery coverage. Each type is tried at its likely scope first and, if
    empty, at the other scope — **lossless** against scope misclassification;
    genuine dual-scope types (`ruleset`, `list`) export at both. ~15–20 min and
    ~1000+ API calls per run.
  - `curated` — a fast, predictable built-in allow-list of common types (~4 min).
- **Parent-keyed child types** that cannot be swept are resolved automatically by
  fetching their parent ids from the API: tunnel ingress config
  (`cloudflare_zero_trust_tunnel_cloudflared_config` ← tunnel ids). Zone settings
  use a static id list (below).
- A type that returns nothing, is not entitled (4xx), or needs a parent id it
  has no data for is a **benign skip** (`EXPORT_MANIFEST.json` → `skipped`); only
  real failures (5xx / 429 / unexpected) land in `errors`. The run never aborts —
  a failed type degrades to a **partial snapshot**. What that means for the run's
  status, alerts and the healthcheck is under [Run status and health](#run-status-and-health).

Override per deployment in the source config:
`resource_types`, `account_resource_types`, `deny_types`, and `resource_ids`
(explicit ids for a parent-keyed type).

> **Known cf-terraforming limitation — R2 bucket sub-configs.** The R2 buckets
> themselves (`cloudflare_r2_bucket`) are backed up, but their sub-configurations
> — `cloudflare_r2_bucket_cors` / `_lifecycle` / `_lock` / `_event_notification`
> / `_sippy` — **cannot** be exported: cf-terraforming 0.27.0 does not substitute
> the bucket name into the API path (it requests a literal `{bucket_name}` and
> gets a 400), regardless of `--resource-id`. They surface as benign skips.
> Revisit when cf-terraforming adds support.

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
| `resource_discovery` | `schema` | `schema` (max coverage) \| `curated` (fast) |
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
(`<id>.tar.gz.age`) is stored/uploaded — unless the encryption itself fails
(tool missing, bad recipient): then the engine stores the **unencrypted** archive
rather than none, and the run ends in `warning` with
`encryption (age) failed, snapshot stored UNENCRYPTED: …` in the alert (see
[Run status and health](#run-status-and-health)). Treat that alert as an
incident: fix the recipient, then delete or re-create that snapshot on every
destination.

## Scheduling

`BACKUP_SCHEDULE_CRON` (default `15 3 * * *`). The container runs a blocking
scheduler; an external scheduler (GitHub Actions / host cron) can instead invoke
`docker compose run --rm cf-backup --now`, which exits 1 when the run ends in
`error` (see [Run status and health](#run-status-and-health)).

Keep the cron at least daily while the `cf-backup` daemon runs: its healthcheck
expects a run every 26 hours (`BACKUP_HEALTHCHECK_MAX_AGE_HOURS`, the image
default, which `docker-compose.yml` does not pass through). With runs further apart
the container is unhealthy from 26 hours after each run until the next one.

## Run status and health

How a failure shows up depends on what failed:

- **Single resource types failed** (5xx / 429 / unexpected): the snapshot holds the
  rest. The failures are listed in `EXPORT_MANIFEST.json` → `errors`, and their
  count is in the component's `errors` field (`show <id>`). They do not change the
  run's status: without other problems the run ends in `success` (exit 0, alert
  only at level `all`).
- **Storing the snapshot went partly wrong** — the S3 upload failed while the local
  copy exists, the encryption fell back to an unencrypted archive, or retention
  failed: the export is complete, so the run ends in `warning`. Exit 0, the alert
  goes out at level `warnings` (the default) and `all`, and the container stays
  healthy.
- **The export failed as a whole** — no token, a token Cloudflare rejects so that
  nothing could be exported, or an exception: the `cloudflare` component fails and
  the run ends in `error`. `--now` exits 1, the alert goes out at every
  `BACKUP_ALERT_LEVEL`, and since BackupHelper 1.7.7 the container turns unhealthy
  until a newer run ends in `success` or `warning`.

The `cf-backup` container's healthcheck (from the BackupHelper engine) is unhealthy
when `/data` is not writable, when the most recent run ended in `error`, left a
failed component or started more than 26 hours ago, or when no backup has run yet
and the daemon started more than 26 hours ago. It prints the reason:

```bash
docker compose exec cf-backup backuphelper healthcheck
# unhealthy: the last backup failed: snapshot 2026-07-05_03-15-00 (job main) at 2026-07-05T03:15:00+00:00: failed component(s): cloudflare
```

A deployment whose newest snapshot already has a failed component is unhealthy
right after the upgrade to 1.7.7, until a complete snapshot exists. The full rules
are in the BackupHelper
[deployment guide](https://github.com/bauer-group/CS-BackupHelper/blob/main/docs/deployment.md#the-functional-healthcheck).

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
