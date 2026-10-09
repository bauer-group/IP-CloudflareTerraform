#!/usr/bin/env bash
# =============================================================================
# CloudflareTerraform backup round trip - check
# =============================================================================
# Exits 0 when the seeded TXT record is in the state ROUNDTRIP_EXPECT names:
#   present  the record holds the marker; once it was tampered with, the mock's
#            request journal must also show that the backup only read from the
#            API and followed the pagination of the zone and record lists, and
#            that the restore wrote the marker back to exactly that record
#   absent   the record holds the tampered content, and nothing was written
#            through the API yet
# In every phase each API request of the image must have carried the token.
# The assertions live in cf-mock/mockctl.py (cmd_check). Once a snapshot exists,
# its manifest must also report all three zones exported without errors.
# =============================================================================
set -euo pipefail
# shellcheck source=tests/backup-roundtrip/common.sh
source "$(dirname "$0")/common.sh"

case "${ROUNDTRIP_EXPECT:?set by the round-trip module}" in
  present | absent) ;;
  *) echo "unknown ROUNDTRIP_EXPECT '$ROUNDTRIP_EXPECT'" >&2; exit 2 ;;
esac

STATUS=0
mockctl check --marker "$ROUNDTRIP_MARKER" --expect "$ROUNDTRIP_EXPECT" || STATUS=1

# The snapshot under test (set by the module once it exists): one file of DNS
# records per zone, and no resource type that failed during the export.
if [ -n "${ROUNDTRIP_SNAPSHOT_ID:-}" ]; then
  EXPECTED='{"zone_count":3,"files_written":3,"errors":0}'
  METADATA=$(docker compose exec -T "${ROUNDTRIP_BACKUP_SERVICE:-cf-backup}" \
      backuphelper show "$ROUNDTRIP_SNAPSHOT_ID" \
    | jq -c '.components[] | select(.name == "cloudflare") | .metadata
             | {zone_count, files_written, errors}')
  if [ "$METADATA" = "$EXPECTED" ]; then
    echo "ok   snapshot $ROUNDTRIP_SNAPSHOT_ID: 3 zones exported, 3 files, no export errors"
  else
    echo "FAIL snapshot $ROUNDTRIP_SNAPSHOT_ID: ${METADATA:-no cloudflare component} (expected $EXPECTED)"
    STATUS=1
  fi
fi

exit "$STATUS"
