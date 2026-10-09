#!/usr/bin/env bash
# =============================================================================
# CloudflareTerraform backup round trip - seed
# =============================================================================
# The mock starts with an account and three zones (see cf-mock/server.py). The
# seed adds, all in charlie.example:
#   * a TXT record holding the run's marker - the third record of the third
#     zone, so it sits on the second page of both lists (the mock pages by two)
#   * a rule in the zone's custom firewall ruleset, described by the marker -
#     rulesets go through cf-terraforming's legacy client
#   * min_tls_version = 1.2, a zone setting
# Then it proves that the mock's TLS endpoint rejects requests without the
# configured token, which the check later relies on when it reports that
# every request carried it.
# =============================================================================
set -euo pipefail
# shellcheck source=tests/backup-roundtrip/common.sh
source "$(dirname "$0")/common.sh"

mockctl seed --marker "$ROUNDTRIP_MARKER"
mockctl probe-auth
