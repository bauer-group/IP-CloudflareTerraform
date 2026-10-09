#!/usr/bin/env bash
# =============================================================================
# CloudflareTerraform backup round trip - mutate
# =============================================================================
# Changes the seeded TXT record behind the backup's back, the way an edit in the
# Cloudflare dashboard would. A changed record, not a deleted one: the restore
# (`cloudflare apply`) reconciles existing resources through the snapshot's
# import blocks, and a deleted record would have to be recreated with --dr.
# =============================================================================
set -euo pipefail
# shellcheck source=tests/backup-roundtrip/common.sh
source "$(dirname "$0")/common.sh"

mockctl tamper --marker "$ROUNDTRIP_MARKER"
