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
(explicit ids for a parent-keyed type). `resource_types` is also exposed in
`.env` as `CLOUDFLARE_RESOURCE_TYPES`: a comma list replaces the zone-level types
of the discovery mode, while account-level types fall back to the curated list —
add `CLOUDFLARE_RESOURCE_SCOPE=zone` to export only the listed types.

> **Known cf-terraforming limitation — R2 bucket sub-configs.** The R2 buckets
> themselves (`cloudflare_r2_bucket`) are backed up, but their sub-configurations
> — `cloudflare_r2_bucket_cors` / `_lifecycle` / `_lock` / `_event_notification`
> / `_sippy` — **cannot** be exported: cf-terraforming 0.27.0 does not substitute
> the bucket name into the API path (it requests a literal `{bucket_name}` and
> gets a 400), regardless of `--resource-id`. They surface as benign skips.
> Revisit when cf-terraforming adds support.
>
> **Curated types cf-terraforming 0.27.0 cannot export.** It has no provider-v5
> endpoint for `cloudflare_workers_script`, so Worker scripts are never exported
> (the call returns nothing, no skip is recorded). `cloudflare_workers_cron_trigger`
> and `cloudflare_authenticated_origin_pulls` need ids (script names, host names)
> the source does not supply; they end as benign skips.
>
> **Snippets are not exported (built-in deny list).** cf-terraforming 0.27.0
> exports `cloudflare_snippets` without the snippet code (`files = []`) and
> without a usable import id, and has no endpoint for its successor
> `cloudflare_snippet`. Provider 5.x keeps `cloudflare_snippets` only as a stub
> whose every operation fails ("use 'cloudflare_snippet' instead"), so a
> restore that included it would fail. The source therefore never exports it,
> in any discovery mode and even when it is listed in `resource_types`. Snippet
> rules (`cloudflare_snippet_rules`) are exported; keep the snippet code itself
> in version control.

**Fixed up for restore.** Where cf-terraforming 0.27 and the provider disagree,
the export adjusts the generated HCL so that `cloudflare apply` plans no change
nobody made — without changing what a restore applies:

- Import blocks are matched to the generated resources by resource id. For
  rulesets the two differ: the API lists managed rulesets, which are not
  generated, and generate sorts by phase. Import blocks without a resource are
  dropped and listed under `skipped` in `EXPORT_MANIFEST.json`.
- Objects the API lists without an `id` get the scope id from cf-terraforming,
  i.e. the import id `<account_id>/<account_id>`, which fails the plan of the
  whole scope. Tunnel ingress configurations (`<account_id>/<tunnel_id>`) and
  R2 buckets (`<account_id>/<name>/<jurisdiction>`) get a correct id built from
  the generated resource. For Turnstile widgets, queues and Web Analytics
  sites the identifying attribute is not in the HCL: their import blocks are
  dropped and the export records an **error** — the snapshot holds their
  definitions, but a restore would create them instead of importing them.
- Keys the API spells differently from the provider are renamed — OpenTofu
  silently drops unknown keys, so a restore would remove those settings:
  the `Host` header of load balancer pool origins (`host`) and the camelCase
  `originRequest` settings of tunnel ingress configurations (`origin_request`,
  `no_tls_verify`, `http_host_header`, …).
- Managed transforms keep only the enabled entries: the provider imports only
  those and disables every other enabled transform on apply.
- Empty values the provider imports as null are dropped: a zone's snippet
  rules resource without rules (cf-terraforming creates one for every zone)
  and a load balancer monitor's `header = {}`.

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
| `resource_types` / `account_resource_types` / `deny_types` | — | overrides (`resource_types` = `CLOUDFLARE_RESOURCE_TYPES`); `deny_types` adds to the built-in deny list (`cloudflare_snippets`) |
| `throttle_rps` | `4` | request/sec ceiling (global limit 1200 / 5 min) |
| `api_base` | `https://api.cloudflare.com/client/v4` | API endpoint of the zone discovery, cf-terraforming and the OpenTofu provider (`CLOUDFLARE_API_BASE_URL`); keep the default — it exists for the [mock API round trip](#round-trip-test-in-ci). A custom endpoint must be https and end in `/client/v4`, or rulesets are not exported: cf-terraforming's legacy ruleset client only follows such an endpoint (as `CLOUDFLARE_API_HOSTNAME`) |
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

## Round-Trip Test in CI

Every release is gated on a real backup and restore — against a mock of the
Cloudflare API, because a CI runner has no Cloudflare account. The job
`🧪 Backup Round Trip` in [docker-release.yml](../.github/workflows/docker-release.yml)
calls the reusable
[`modules-backup-roundtrip-test.yml`](https://github.com/bauer-group/automation-templates/blob/main/docs/workflows/modules-backup-roundtrip-test.md)
and runs before the release job, which needs it to pass. It also runs when the
base image monitor dispatches a release after a new BackupHelper engine image, so
an engine update ships only after it backed up and restored Cloudflare
configuration.

The export runs with the plugin's **curated default resource types at zone and
account scope** (`CLOUDFLARE_RESOURCE_DISCOVERY=curated`,
`CLOUDFLARE_RESOURCE_SCOPE=all`, no type override). Schema discovery is not used
in CI: it asks for ~250 types per scope and would turn the gate into a
half-hour job.

### The mock API

The mock ([`tests/backup-roundtrip/cf-mock/server.py`](../tests/backup-roundtrip/cf-mock/server.py),
Python standard library on `python:3-alpine`) is added by the CI-only override
[`tests/backup-roundtrip/docker-compose.ci.yml`](../tests/backup-roundtrip/docker-compose.ci.yml).
It serves every endpoint the curated list needs, with Cloudflare's response
envelope; accepts only the `CLOUDFLARE_API_TOKEN` generated for the run; returns
the lists the real API paginates in pages of two; and records each request in a
journal — with the client that sent it (from the User-Agent), the route that
answered and whether a write changed anything.

| Scope | Resources with data |
| --- | --- |
| Account | `cloudflare_ruleset` (a root entry point that executes a custom ruleset, plus a listed managed ruleset), three `cloudflare_workers_kv_namespace` (two pages), a `cloudflare_zero_trust_tunnel_cloudflared` and its `_config` with `originRequest` settings, a `cloudflare_load_balancer_monitor` and `_pool` (origin with a `Host` header), a `cloudflare_r2_bucket` |
| Every zone | DNS records, the 27 default `cloudflare_zone_setting` ids, `cloudflare_bot_management`, `cloudflare_url_normalization_settings`, `cloudflare_managed_transforms`, a listed managed ruleset |
| `charlie.example` | its custom firewall ruleset (zone entry point), a `cloudflare_load_balancer` on the account's pool |

Every other curated type answers with an empty list; a request to anything else
is a 404 that fails the check. Ids are derived at runtime and the token is
generated per run; neither is committed.

**TLS.** cf-terraforming 0.27 still lists rulesets with its legacy client, which
ignores `CLOUDFLARE_BASE_URL` and only calls `https://<CLOUDFLARE_API_HOSTNAME>/client/v4`.
So the mock speaks TLS on `cf-mock:8443`, and the source's `api_base` is
`https://cf-mock:8443/client/v4`, which the plugin hands to that client as
`CLOUDFLARE_API_HOSTNAME`. [`prepare.sh`](../tests/backup-roundtrip/prepare.sh)
— the module's `prepare-script` — creates a CA and a certificate for `cf-mock`
per run in `tests/backup-roundtrip/.tls/` (git-ignored) and deletes the CA key
right away. `cf-backup` trusts that CA through `SSL_CERT_DIR`, in addition to its
own CA bundle, so Python, cf-terraforming, OpenTofu and the provider accept the
mock's certificate without any of them skipping verification. Inside the CI stack `api.cloudflare.com` resolves to `127.0.0.1`, so
a request that bypassed the mock would fail instead of carrying the token to the
real API. The mock's control plane (seed, tamper, journal) listens on
`127.0.0.1:8080` inside its container and cannot be reached from `cf-backup`.

### Phases

| Phase | What happens |
| --- | --- |
| Build | `src/cloudflare-backup` is built from the commit, `FROM` the newest engine, with the pytest gate |
| Prepare | `prepare.sh` creates the TLS material; `.env` sets `CLOUDFLARE_API_BASE_URL=https://cf-mock:8443/client/v4`, the curated discovery at both scopes and a generated `CLOUDFLARE_API_TOKEN` |
| Seed | In `charlie.example`: a TXT record `_roundtrip.charlie.example` holding the run's marker (third record of the third zone, so on page 2 of both lists), a rule of the custom firewall ruleset described by the marker, and `min_tls_version = 1.2`. Requests to the TLS endpoint without a token or with a wrong one must be rejected |
| Back up | `create` must exit `0`, `show` must list `cloudflare` without errors or warnings, `verify` must report `OK` |
| Tamper | The record's content, the rule's description (and the rule disabled) and `min_tls_version = 1.0` — the way edits in the dashboard would change them |
| Restore | `cloudflare apply <id> --zone charlie.example --force`: import blocks, plan, apply, re-plan |
| Health | `backuphelper healthcheck` must report the sidecar `healthy` |

The check ([`check.sh`](../tests/backup-roundtrip/check.sh), assertions in
`cf-mock/mockctl.py`) runs three times: after the seed (marker data present),
after the tamper (tampered values present, nothing written) and after the
restore (marker data back). Each time every API request of the image must carry
the token and hit a route the mock models. Once the snapshot exists, its export
must cover the three zones and the account, hold exactly the expected 29 `.tf`
files and record no export error. In addition:

- **After the tamper**: `cloudflare drift --against <id> --zone charlie.example`
  must exit `1` and report exactly the three tampered files — a second export
  reproduces every other file of the zone and the account.
- **After the restore**: the journal must show that the backup only read; asked
  for every curated type the mock serves; followed page 2 of the zone list, of
  `charlie.example`'s records and of the KV namespaces; listed the rulesets of
  all three zones and the account with cf-terraforming's legacy client through
  the mock and fetched the rules of the three custom rulesets. The restore must
  have written the three seeded values back and changed nothing else (no-op
  writes are listed), every write must have succeeded. Finally
  `cloudflare apply <id> --plan-only` must import every resource and plan no
  change, for `charlie.example` (35 imports) and for the account (10 imports).

The restore step is `cloudflare apply`, the command an operator runs: the
engine's own `restore` does not handle the `cloudflare` component (see
[RESTORE-RUNBOOK.md](RESTORE-RUNBOOK.md)). The test changes resources instead
of deleting them, because `apply` reconciles existing resources through the
snapshot's import blocks; deleted ones need `--dr`.

**Not covered.** Schema discovery; the account scope is planned, not applied;
`--dr`; curated types the mock serves as empty lists (page rules, lists,
Access, Gateway, Turnstile, queues, …) are only asked for, their HCL is not
exercised; `cloudflare diff` between two snapshots (drift uses the
same diff code). The mock follows the shape of the real responses for these
endpoints, not every rule of the real API (for example, it does not validate
DNS record contents or ruleset expressions).

A run takes about 4 minutes. It starts on pushes to `main` (documentation-only
pushes excluded), on every `workflow_dispatch`, and on pull requests that touch
`src/`, a compose file, `.env.example`, `tests/backup-roundtrip/` or the release
workflow. When it fails, the step summary names the failed phase, and the
`backup-roundtrip-diagnostics` artifact holds the logs of `cf-backup` and
`cf-mock` (one line per request), `docker compose ps`, the snapshot list and
the output of `create` and of the restore.

Outside CI, run `bash tests/backup-roundtrip/prepare.sh` before starting the
stack with the override: without the TLS material `cf-mock` exits at once.
