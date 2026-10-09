#!/usr/bin/env bash
# =============================================================================
# CloudflareTerraform backup round trip - mutate
# =============================================================================
# Changes the seeded data behind the backup's back, the way edits in the
# Cloudflare dashboard would: the TXT record's content, the marker rule's
# description (and disables it), and min_tls_version back to 1.0. Changed, not
# deleted: the restore (`cloudflare apply`) reconciles existing resources
# through the snapshot's import blocks; deleted ones would need --dr.
# =============================================================================
set -euo pipefail
# shellcheck source=tests/backup-roundtrip/common.sh
source "$(dirname "$0")/common.sh"

mockctl tamper --marker "$ROUNDTRIP_MARKER"
