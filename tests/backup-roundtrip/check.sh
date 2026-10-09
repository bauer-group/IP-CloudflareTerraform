#!/usr/bin/env bash
# =============================================================================
# CloudflareTerraform backup round trip - check
# =============================================================================
# Exits 0 when the seeded data is in the state ROUNDTRIP_EXPECT names:
#   present  the TXT record, the marker rule and min_tls_version hold what the
#            seed put there; once they were tampered with, the mock's request
#            journal must also show that the backup only read from the API,
#            asked for every curated resource type, followed the pagination,
#            listed the rulesets with cf-terraforming's legacy client through
#            the mock, and that the restore changed exactly the tampered
#            resources back
#   absent   all three hold the tampered values, and nothing was written
#            through the API yet
# In every phase each API request of the image must have carried the token
# and hit a route the mock models. The assertions live in cf-mock/mockctl.py.
#
# Once the snapshot exists (ROUNDTRIP_SNAPSHOT_ID), it also checks
#   * its export: all zones and the account, exactly the expected .tf files,
#     no export errors (manifest.py reads it inside cf-backup)
#   * after the tamper: `cloudflare drift` against the snapshot reports
#     exactly the three tampered files of charlie.example
#   * after the restore: `cloudflare apply --plan-only` for charlie.example and
#     for the account imports every resource and plans no change
# =============================================================================
set -euo pipefail
# shellcheck source=tests/backup-roundtrip/common.sh
source "$(dirname "$0")/common.sh"

case "${ROUNDTRIP_EXPECT:?set by the round-trip module}" in
  present | absent) ;;
  *) echo "unknown ROUNDTRIP_EXPECT '$ROUNDTRIP_EXPECT'" >&2; exit 2 ;;
esac

MARKER_ZONE=charlie.example
SNAPSHOT="${ROUNDTRIP_SNAPSHOT_ID:-}"
STATUS=0

mockctl check --marker "$ROUNDTRIP_MARKER" --expect "$ROUNDTRIP_EXPECT" || STATUS=1

# The snapshot's export, as stored (verified and extracted like `cloudflare
# apply` does it).
check_snapshot() {
  local exported
  if ! exported=$(docker compose exec -T "$BACKUP_SERVICE" python - "$SNAPSHOT" \
      < "$ROUNDTRIP_SCRIPTS/manifest.py"); then
    echo "FAIL could not read the export of snapshot $SNAPSHOT"
    return 1
  fi
  mockctl snapshot <<< "$exported"
}

# A fresh export against the snapshot: only the tampered resources differ.
check_drift() {
  local out rc counts changed expected
  set +e
  out=$(bh cloudflare drift --against "$SNAPSHOT" --zone "$MARKER_ZONE" 2>&1)
  rc=$?
  set -e
  counts=$(grep -o -E 'changed: [0-9]+ +added: [0-9]+ +removed: [0-9]+' <<< "$out" | head -n 1 || true)
  changed=$(sed -n -E "s|^--- $SNAPSHOT/(.+\\.tf)\$|\\1|p" <<< "$out" | sort | tr '\n' ' ')
  expected=$(printf 'zones/%s/%s.tf\n' "$MARKER_ZONE" cloudflare_dns_record \
    "$MARKER_ZONE" cloudflare_ruleset "$MARKER_ZONE" cloudflare_zone_setting | sort | tr '\n' ' ')
  if [ "$rc" -eq 1 ] && [[ "$counts" =~ ^changed:\ 3\ +added:\ 0\ +removed:\ 0$ ]] \
      && [ "$changed" = "$expected" ]; then
    echo "ok   cloudflare drift against $SNAPSHOT reports exactly the tampered files: ${changed% }"
    return 0
  fi
  echo "FAIL cloudflare drift against $SNAPSHOT: exit $rc (expected 1), ${counts:-no summary}," \
       "changed files: ${changed:-none} (expected ${expected% })"
  tail -n 60 <<< "$out"
  return 1
}

# `cloudflare apply --plan-only`: every resource of the scope is imported and
# nothing is left to change.
check_plan() {
  local label="$1" out summary
  shift
  if ! out=$(bh cloudflare apply "$SNAPSHOT" "$@" --plan-only 2>&1); then
    echo "FAIL plan of $label: cloudflare apply --plan-only failed"
    tail -n 60 <<< "$out"
    return 1
  fi
  summary=$(grep -m 1 -E '^(Plan: |No changes\.)' <<< "$out" || true)
  if [[ "$summary" =~ ^Plan:\ [0-9]+\ to\ import,\ 0\ to\ add,\ 0\ to\ change,\ 0\ to\ destroy\.$ ]]; then
    echo "ok   plan of $label from $SNAPSHOT changes nothing: $summary"
    return 0
  fi
  echo "FAIL plan of $label from $SNAPSHOT: ${summary:-no plan summary}"
  grep -E '^ *# |^ +[-+~] |Error|error' <<< "$out" | head -n 120
  return 1
}

if [ -n "$SNAPSHOT" ]; then
  check_snapshot || STATUS=1
  if [ "$ROUNDTRIP_EXPECT" = absent ]; then
    check_drift || STATUS=1
  else
    check_plan "$MARKER_ZONE" --zone "$MARKER_ZONE" || STATUS=1
    check_plan "the account" --account "$(mockctl account-id)" || STATUS=1
  fi
fi

exit "$STATUS"
