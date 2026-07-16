# Diff & drift

## Compare two backups

```bash
docker compose run --rm cf-backup cloudflare diff <id-a> <id-b>
```

- Extracts both snapshots (pulling from off-site S3 first if not local, verifying
  each against its sha256), then does a **normalized, offline** unified diff of
  the `.tf` trees. No API calls.
- Both snapshots were `tofu fmt`-canonicalized at backup time, so the diff is
  order-stable and free of formatting noise.
- `--raw` compares verbatim (including whitespace); `--exit-code`/`-x` exits `1`
  when the snapshots differ (for scripting/CI).

Output summarizes changed / added / removed files, then the per-file diff:

```text
changed: 1  added: 0  removed: 0

--- 2026-07-15_03-15-00/zones/example.com/cloudflare_dns_record.tf
+++ 2026-07-16_03-15-00/zones/example.com/cloudflare_dns_record.tf
@@ ...
-  content = "1.2.3.4"
+  content = "5.6.7.8"
```

### Version gating

If the two snapshots were produced by different tool/provider versions (recorded
in each `EXPORT_MANIFEST.json`), the command prints a warning — schema churn
between provider versions can make a cross-version diff noisy. Compare snapshots
from the same provider line when possible.

## Drift — what changed since the last backup

```bash
docker compose run --rm cf-backup cloudflare drift
docker compose run --rm cf-backup cloudflare drift --zone example.com
docker compose run --rm cf-backup cloudflare drift --against <id>
```

`drift` exports the account **now** to a temporary tree and diffs it against the
newest stored snapshot (or `--against <id>`). Unlike `diff` (two historical
backups), drift needs live API access — it answers *"has Cloudflare changed since
my last backup?"*. Exit `1` when drift is found.

Use `--zone` to scope a fast check to a single zone; `--raw` for a verbatim diff.
