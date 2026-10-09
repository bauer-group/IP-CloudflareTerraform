#!/usr/bin/env bash
# =============================================================================
# CloudflareTerraform backup round trip - seed
# =============================================================================
# The mock starts with three zones and four DNS records. The seed adds a TXT
# record whose content is the run's marker: the third record of the third zone,
# so it sits on the second page of both lists (the mock pages by two). Then it
# proves that the mock rejects requests without the configured token, which the
# check later relies on when it reports that every request carried it.
# =============================================================================
set -euo pipefail
# shellcheck source=tests/backup-roundtrip/common.sh
source "$(dirname "$0")/common.sh"

mockctl seed --marker "$ROUNDTRIP_MARKER"
mockctl probe-auth
