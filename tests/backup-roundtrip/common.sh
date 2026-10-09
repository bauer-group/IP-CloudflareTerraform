#!/usr/bin/env bash
# =============================================================================
# CloudflareTerraform backup round trip - shared helpers (sourced, not executed)
# =============================================================================
# Used by seed.sh, mutate.sh and check.sh, which the automation-templates module
# modules-backup-roundtrip-test.yml runs against the started stack:
# docker-compose.yml plus docker-compose.ci.yml in this directory, which adds a
# mock of the Cloudflare API (service cf-mock). The module exports COMPOSE_FILE
# and COMPOSE_PROJECT_NAME, so a plain `docker compose` reaches this stack, plus
# ROUNDTRIP_MARKER - a unique token ([a-z0-9-]) per run.
# =============================================================================

: "${ROUNDTRIP_MARKER:?set by the round-trip module}"

# The directory of these scripts (manifest.py is fed from it).
# shellcheck disable=SC2034 # used by the scripts that source this file
ROUNDTRIP_SCRIPTS="$(dirname "${BASH_SOURCE[0]}")"
BACKUP_SERVICE="${ROUNDTRIP_BACKUP_SERVICE:-cf-backup}"

# Runs the mock's control CLI (cf-mock/mockctl.py) inside the cf-mock container.
# The marker reaches it as an argument, never pasted into code.
mockctl() {
  docker compose exec -T cf-mock python /mock/mockctl.py "$@"
}

# Runs the BackupHelper CLI (with the cloudflare plugin) inside cf-backup.
bh() {
  docker compose exec -T "$BACKUP_SERVICE" backuphelper "$@"
}
