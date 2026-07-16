# Restore runbook — applying a backup to Cloudflare

Restore = pushing a snapshot's HCL back to Cloudflare with `tofu apply`. This is
**destructive** and therefore **plan-gated** by default: plan → human review →
apply → re-plan. One scope (a single zone, or an account) per run.

> The engine's own `restore <id>` only rehydrates the HCL files onto disk; it does
> **not** push to Cloudflare. Use `cloudflare apply` for that.

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
