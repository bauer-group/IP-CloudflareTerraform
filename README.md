# CloudflareTerraform

**Backup, restore and diff your Cloudflare configuration as OpenTofu/Terraform code.**

A toolkit that exports a whole Cloudflare account to Terraform HCL with
[`cf-terraforming`](https://github.com/cloudflare/cf-terraforming), stores
versioned, integrity-checked, off-site snapshots via the BAUER GROUP
**BackupHelper** engine, and adds Cloudflare-specific verbs to **diff** two
backups, **apply** a backup back to Cloudflare, and detect **drift**.

Everything runs in one Docker image (OpenTofu + cf-terraforming + a BackupHelper
plugin). No local toolchain beyond Docker.

---

## Two modes, one image

| Mode | How | What it does |
| --- | --- | --- |
| **Backup** (scheduled) | `docker compose up -d cf-backup` | Engine scheduler exports the account on cron → deterministic `tar.gz` + sha256 manifest → **off-site S3** → **retention 30 days + GFS** → alerting. |
| **CLI** (interactive) | `docker compose run --rm cf-backup <verb>` | Engine verbs (`--now`, `list`, `verify`, `restore`, `prune`) **plus** `cloudflare diff` / `apply` / `drift` / `export`. |

## Quick start

```bash
cp .env.example .env          # fill in CLOUDFLARE_API_TOKEN (+ optional S3, alerts)
docker compose build

# take a backup now
docker compose run --rm cf-backup --now
docker compose run --rm cf-backup list

# run scheduled backups (cron from BACKUP_SCHEDULE_CRON)
docker compose up -d cf-backup
```

## Command reference

```bash
# engine (BackupHelper) verbs
cf-backup --now                       # export + snapshot now
cf-backup list                        # list snapshots (local + off-site)
cf-backup verify <id>                 # sha256 integrity check
cf-backup restore <id>                # rehydrate the HCL files from a snapshot (not a push)
cf-backup prune                       # apply retention now
cf-backup config                      # print effective config (secrets redacted)

# cloudflare verbs (this repo)
cf-backup cloudflare diff <id-a> <id-b> [--raw] [--exit-code]   # offline diff of two backups
cf-backup cloudflare apply <id> --zone <name> [--dr] [--force]  # restore → Cloudflare (plan-gated)
cf-backup cloudflare drift [--zone <name>] [--against <id>]     # changes since the last backup
cf-backup cloudflare export --out <dir>                         # ad-hoc HCL export (no snapshot)
```

(Prefix each with `docker compose run --rm`.)

## How it works

```text
                    ┌──────────────────────── cloudflare-backup image ───────────────────────┐
Cloudflare API ──▶  │  cloudflare source  ──▶  cf-terraforming generate ──▶ OpenTofu HCL tree │
                    │        (tofu init reads the provider schema; token via env)             │
                    │                                   │ bundle (cloudflare.tar.gz)          │
                    │        BackupHelper engine  ◀──────┘                                     │
                    │  sha256 manifest · deterministic tar.gz · age/gpg · S3 · retention · alert
                    └────────────────────────────────────────────────────────────────────────┘
                                   │                              ▲
                       snapshots (/data + S3)          cloudflare diff / apply / drift
```

- The **`cloudflare` source** is a BackupHelper plugin: it exports, the engine
  does all the backup mechanics (integrity, bundling, encryption, S3, retention,
  alerting). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- `cf-terraforming` drives OpenTofu via `--terraform-binary-path` and reads the
  provider schema from an initialized working dir — both binaries live in the
  same image so they share it.
- Snapshot ids are UTC `%Y-%m-%d_%H-%M-%S`; versioning, retention (30 days + GFS)
  and off-site replication are the engine's, not git's.

## Documentation

| Doc | Topic |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the pieces fit; the plugin contract; design decisions |
| [docs/BACKUP.md](docs/BACKUP.md) | Backup mode, config, scope, retention, scheduling |
| [docs/DIFF.md](docs/DIFF.md) | Comparing two backups; drift detection |
| [docs/RESTORE-RUNBOOK.md](docs/RESTORE-RUNBOOK.md) | Restoring to Cloudflare (plan-gated + `--force`), secret re-injection |
| [docs/SECRETS-MANIFEST.md](docs/SECRETS-MANIFEST.md) | Resources whose secrets do not round-trip |
| [src/cloudflare-backup/README.md](src/cloudflare-backup/README.md) | The image itself |

## Security

- API token via `CLOUDFLARE_API_TOKEN` env only — never in the rendered config,
  never on a command line. Use a **read-only** token for backups and a separate
  **read-write** token for `cloudflare apply`.
- Off-site S3 has **no server-side encryption** in the engine — set
  `BACKUP_ENCRYPTION_MODE=age` for at-rest encryption of sensitive backups.
- State/secret hygiene: `.gitignore` blocks `.env`, `*.tfstate*`, `.terraform/`.
- Secrets that do not round-trip (Access tokens, tunnels, certs, API tokens,
  Worker secrets) are reported per backup and must be re-injected on restore —
  see [docs/SECRETS-MANIFEST.md](docs/SECRETS-MANIFEST.md).

## Requirements

Docker (Desktop on Windows/macOS, Engine on Linux). Pinned inside the image:
OpenTofu ≥ 1.11 (write-only attributes), `cf-terraforming` 0.27.0, Cloudflare
provider `>= 5.8.2, < 6.0.0`.

## License

MIT © BAUER GROUP — see [LICENSE](LICENSE).
