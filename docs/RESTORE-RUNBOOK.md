# Restore runbook — applying a backup to Cloudflare

Restore = pushing a snapshot's HCL back to Cloudflare with `tofu apply`. This is
**destructive** and therefore **plan-gated** by default: plan → human review →
apply → re-plan. One scope (a single zone, or an account) per run.

> The engine's own `restore <id>` does **not** restore this stack: the `cloudflare`
> source implements no engine restore, so the command logs
> `restore of component cloudflare failed: cloudflare source does not support restore`,
> ends with `restore finished with errors` (exit 1) and changes nothing. Use
> `cloudflare apply` to push a snapshot to Cloudflare, and `download <id> <dir>` to
> copy a snapshot's archive out of the volume.

## What a snapshot contains

With the default `schema` discovery a snapshot holds **100+ resource types** split
into `zones/<name>/` and `_account/<id>/`. `cloudflare apply` restores **one scope
at a time** — apply each zone and the account separately, reviewing each plan.

**Order matters for parent-keyed resources.** Child resources reference a parent
that must exist first:

- **Tunnels:** apply `cloudflare_zero_trust_tunnel_cloudflared` (+ re-inject the
  `tunnel_secret`) before its `_config` (ingress) / `_route` / `_virtual_network`.
- **R2:** apply `cloudflare_r2_bucket` before its `_cors` / `_lifecycle` / `_lock`
  / `_event_notification` / `_sippy`.

For a `--dr` (from-scratch) restore, apply parents first, then re-run for the
children; for drift-correction the `import{}` blocks reconcile existing parents.

## Preconditions

- A **read-write** `CLOUDFLARE_API_TOKEN` (the backup token is read-only). Set it
  in the environment for the run.
- Know the target scope: `--zone <name>` or `--account <id>`.
- Read the secrets report for the snapshot (below) — some values must be
  re-injected or the plan will show spurious replacements.

## Modes

| Command | Behaviour |
| --- | --- |
| `cloudflare apply <id> --zone <z>` | drift-correction: `import{}` blocks reconcile existing resources into state, then plan/apply. Interactive approval required. |
| `cloudflare apply <id> --zone <z> --dr` | disaster recovery: recreate from scratch (import blocks omitted). |
| `cloudflare apply <id> --zone <z> --plan-only` | show the plan and stop — never applies. |
| `cloudflare apply <id> --zone <z> --force` | unattended reconcile (no prompt) — for GitOps automation. |

## Procedure (drift-correction)

```bash
export CLOUDFLARE_API_TOKEN=<read-write token>

# 1. Review the plan first (no changes made)
docker compose run --rm -e CLOUDFLARE_API_TOKEN cf-backup \
  cloudflare apply <id> --zone example.com --plan-only

# 2. Re-inject any non-round-tripping secrets flagged by the plan (see below)

# 3. Apply with human approval
docker compose run --rm -e CLOUDFLARE_API_TOKEN cf-backup \
  cloudflare apply <id> --zone example.com
#    → shows the plan + secret warnings, asks: "Apply this plan to Cloudflare …?"

# 4. The command re-plans after apply — confirm it reports no further changes.
```

For automated reconcile, replace step 3 with `--force` (skips the prompt). Never
run `--force` against production without first reviewing a `--plan-only` run.

**Creates in a drift-correction plan.** A drift-correction imports the live
resources first; a resource that plans as `will be created` had no import
block. If its live object still exists, the apply creates a second one or
fails on a name conflict. The export lists the known cases under `errors` in
the snapshot's `EXPORT_MANIFEST.json` ("without an import id" / "no import
id"): Turnstile widgets (a new widget gets a new site key), queues and Web
Analytics sites — see [BACKUP.md](BACKUP.md#what-gets-exported). `--force`
does not check for them, so do not run it unattended on such a scope: review
the plan and approve it only if those objects are really gone.

This drift-correction path (`--zone <z> --force`) runs on every release against
a mock Cloudflare API — DNS records, a ruleset and a zone setting are restored,
then `--plan-only` must find nothing left to change for the zone and for the
account; see [BACKUP.md](BACKUP.md#round-trip-test-in-ci).

## MANDATORY: re-inject non-round-tripping secrets

Some resources export their **definition** but not their **secret payload** — the
Cloudflare API never returns it. A plain apply would show a spurious `replace`.
Before/after apply you must re-supply the secret out-of-band (via a variable, a
secret manager, or the dashboard):

| Resource | Value to re-inject |
| --- | --- |
| `cloudflare_zero_trust_access_service_token` | `client_secret` (shown once at creation) |
| `cloudflare_zero_trust_tunnel_cloudflared` | `tunnel_secret` |
| `cloudflare_origin_ca_certificate` / `cloudflare_custom_ssl` / `cloudflare_mtls_certificate` | private key material |
| `cloudflare_api_token` / `cloudflare_account_token` | token value (minted anew on re-apply) |
| `cloudflare_workers_secret` | secret text (write-only) |

`cloudflare apply` prints the specific list for the scope it is applying. The
per-backup list is also in each snapshot's `EXPORT_MANIFEST.json` →
`secrets_report`. See [SECRETS-MANIFEST.md](SECRETS-MANIFEST.md).

## Out of scope

Data-plane content is **not** configuration and is not restored: Workers KV
values, R2 objects, D1 rows, Stream media. Back those up with dedicated jobs.

## If something goes wrong

- The plan step is non-destructive — always start there.
- A failed apply leaves state partially reconciled; re-run `--plan-only` to see
  the remaining delta before retrying.
- Keep the previous snapshot: you can always `diff` the current export against a
  known-good backup to understand what changed.
