# Architecture

## Why this shape

The toolkit is built **on** the central BackupHelper engine
(`ghcr.io/bauer-group/cs-backuphelper/backuphelper`) rather than reimplementing
backup mechanics. The engine already owns everything generic:

- streamed sha256 + a dual manifest (embedded + sidecar),
- byte-deterministic `tar.gz` bundling,
- optional client-side `age`/`gpg` encryption,
- boto3 off-site S3 (path-style + SigV4; AWS/MinIO/R2/B2/Wasabi),
- retention (`count` / `age_days` / **GFS** / `smart_last`), applied per destination,
- alerting (email / webhook / Teams / Slack / Discord / ntfy / healthchecks),
- the snapshot CLI (`--now`, `list`, `show`, `verify`, `download`, `restore`, `prune`).

We contribute only the Cloudflare-specific parts, as a pip plugin the engine
discovers via entry points.

## The one image

```text
FROM ghcr.io/opentofu/opentofu:1.12.4-minimal AS tofu   # lift the tofu binary
FROM ghcr.io/bauer-group/cs-backuphelper/backuphelper   # the engine
  + /usr/local/bin/tofu            (multi-stage COPY — no ONBUILD trigger)
  + /usr/local/bin/cf-terraforming (pinned release; version-checked at build = musl smoke test)
  + pip install backuphelper_cloudflare   (registers the entry points; pytest gate)
```

No official `cf-terraforming` image exists and the OpenTofu image is
`FROM scratch`, so a single custom image that carries **both** binaries is the
only workable shape — cf-terraforming shells out to `tofu` and reads the provider
schema from a shared, initialized working dir.

## Plugin entry points

| Group | Registration | Effect |
| --- | --- | --- |
| `backuphelper.sources` | `cloudflare = backuphelper_cloudflare.source:CloudflareSource` | adds a `cloudflare` backup source |
| `backuphelper.commands` | `cloudflare = backuphelper_cloudflare.commands:app` | mounts `backuphelper cloudflare <verb>` |

## Backup data flow

1. The engine scheduler (or `--now`) invokes the `cloudflare` **source**.
2. `source.produce(staging_dir)` runs the **export core** into a temp tree:
   - write a pinned provider `main.tf`, `tofu init` (populates the provider schema),
   - read the authoritative resource list from `tofu providers schema -json`,
   - discover zones via the Cloudflare API (`/zones`, paginated),
   - loop the selected resource types running `cf-terraforming generate`
     (`-z` per zone, `-a` per account) and `import --modern-import-block`,
     throttled to `throttle_rps`,
   - `tofu fmt` to canonicalize, write `EXPORT_MANIFEST.json`.
3. `produce` packages the tree as one deterministic `cloudflare.tar.gz`
   `StagedComponent` (with metadata: zone count, tool versions, secrets report).
4. The engine hashes, bundles, (optionally encrypts,) uploads to every
   destination, applies retention, and notifies.

## CLI verbs

`commands.py` reuses the engine's own front-half (`_hydrate_from_destinations`,
the sha256 gate, `_decrypt_if_needed`, `extract_bundle`) via `snapshot.open_export`,
so snapshot access can never diverge from how the engine wrote it.

- **diff** — extract two snapshots, normalized `difflib` diff of the `.tf` trees.
- **apply** — stage one scope, `tofu init` → `plan` → approval (or `--force`) →
  `apply` → re-`plan`. `import{}` blocks reconcile live resources; `--dr` skips them.
- **drift** — export now to a temp tree, diff against the newest stored snapshot.

## Layered, testable code

Only `source.py`, `snapshot.py` and `commands.py` import the engine (provided by
the base image). The core — `config`, `cfapi`, `tofu`, `cfterraforming`,
`export`, `diff`, `apply`, `resources` — imports nothing from the engine, so it
unit-tests on any host (38 tests). Every subprocess and HTTP call is injectable.

## Key design decisions

- **Robustness over hardcoded resource names.** v5 renamed everything; the
  exporter reads the resource set from the live provider schema and skips
  unknown types with a warning, so a stale curated name degrades gracefully.
- **Storage is the engine's, not git's.** Versioning, retention and off-site
  replication come from BackupHelper snapshots; nothing backup-related is
  committed. `diff` compares two snapshots.
- **Restore is never automatic.** Pushing HCL to Cloudflare is `tofu apply`
  (destructive), so it is a plan-gated CLI verb, not the engine's restore loop.

## Pins

| Component | Pin | Why |
| --- | --- | --- |
| OpenTofu | ≥ 1.11 (image 1.12.4) | provider-v5 write-only attributes |
| cf-terraforming | 0.27.0 | first line with full provider-v5 support (≥ 0.24.0) |
| Cloudflare provider | `>= 5.8.2, < 6.0.0` | matches the fleet's existing module (validated v5.21.1) |
