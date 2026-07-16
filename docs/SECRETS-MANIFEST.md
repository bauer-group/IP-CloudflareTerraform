# Secrets manifest — what does not round-trip

`cf-terraforming` exports the **configuration** of a resource, but the Cloudflare
API never returns certain **secret values**. Those resources back up fine, but a
restore (`cloudflare apply`) cannot recreate the secret — it must be re-supplied
out-of-band. This page is the reference; each backup also emits its own concrete
list in `EXPORT_MANIFEST.json` → `secrets_report`, and `cloudflare apply` prints
the list for the scope it touches.

## Non-round-tripping resources

| Resource | Lost value | On restore |
| --- | --- | --- |
| `cloudflare_zero_trust_access_service_token` | `client_secret` (shown once at creation) | re-issue the token or supply the stored secret |
| `cloudflare_zero_trust_tunnel_cloudflared` | `tunnel_secret` | supply the tunnel secret; otherwise the tunnel is replaced |
| `cloudflare_origin_ca_certificate` | private key | re-supply the key / re-issue the cert |
| `cloudflare_custom_ssl` | private key | re-upload the certificate + key |
| `cloudflare_mtls_certificate` | private key | re-upload |
| `cloudflare_api_token` | token value | a re-apply mints a **new** token value |
| `cloudflare_account_token` | token value | as above |
| `cloudflare_workers_secret` | secret text (write-only) | re-set the Worker secret |

## How to handle it

1. **Store the real secrets in a secret manager** (Vault/OpenBao, a cloud KMS, or
   `sops`/`age`-encrypted files kept out of git) — not in the backup, not in git.
2. **At restore**, re-inject via Terraform variables / `tofu` `-var`, the
   dashboard, or an API call, per the runbook
   ([RESTORE-RUNBOOK.md](RESTORE-RUNBOOK.md)).
3. **Encrypt backups at rest** anyway (`BACKUP_ENCRYPTION_MODE=age`) — exported
   HCL can still contain sensitive-but-returned config.

## Out of scope entirely (not configuration)

The following are **data**, not config, and are not part of a Terraform export.
Back them up with dedicated jobs if you need them:

- Workers **KV** values, **R2** objects, **D1** rows, **Durable Object** state,
- **Stream** media, **Images**,
- logs / analytics.
