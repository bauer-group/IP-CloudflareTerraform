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
# The assertions live in cf-mock/mockctl.py (cmd_check).
# =============================================================================
set -euo pipefail
# shellcheck source=tests/backup-roundtrip/common.sh
source "$(dirname "$0")/common.sh"

case "${ROUNDTRIP_EXPECT:?set by the round-trip module}" in
  present | absent) ;;
  *) echo "unknown ROUNDTRIP_EXPECT '$ROUNDTRIP_EXPECT'" >&2; exit 2 ;;
esac

mockctl check --marker "$ROUNDTRIP_MARKER" --expect "$ROUNDTRIP_EXPECT"
