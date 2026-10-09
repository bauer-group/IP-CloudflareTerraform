#!/usr/bin/env bash
# =============================================================================
# CloudflareTerraform backup round trip - prepare
# =============================================================================
# Creates the TLS material of the mock Cloudflare API for this run, before the
# stack starts (the round-trip module runs it as its prepare-script; run it by
# hand before a local `docker compose up` of the CI stack).
#
# The mock has to speak https: cf-terraforming's legacy client, which it still
# uses for cloudflare_ruleset, always calls "https://<CLOUDFLARE_API_HOSTNAME>
# /client/v4". So this script generates a throwaway CA and a certificate for
# the host name cf-mock, valid for two days, into tests/backup-roundtrip/.tls/
# (git-ignored):
#
#   server/   cert.pem, key.pem and ca.pem - mounted into cf-mock
#   ca/       the CA certificate under its OpenSSL hash name - mounted into
#             cf-backup and added to its trust store with SSL_CERT_DIR
#
# The CA key never leaves a temporary directory and is deleted on exit, so no
# further certificate can be issued under this CA. Nothing here is committed or
# reused: every run creates new keys.
# =============================================================================
set -euo pipefail

TLS_DIR="$(dirname "$0")/.tls"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

rm -rf "$TLS_DIR"
mkdir -p "$TLS_DIR/server" "$TLS_DIR/ca"

openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 2 \
  -subj "/CN=cf-mock round-trip CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout "$WORK/ca.key" -out "$TLS_DIR/server/ca.pem"

openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -subj "/CN=cf-mock" -keyout "$TLS_DIR/server/key.pem" -out "$WORK/server.csr"

cat > "$WORK/server.ext" << 'EXT'
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = DNS:cf-mock
EXT
openssl x509 -req -in "$WORK/server.csr" -CA "$TLS_DIR/server/ca.pem" -CAkey "$WORK/ca.key" \
  -set_serial "0x$(openssl rand -hex 16)" -days 2 -sha256 -extfile "$WORK/server.ext" \
  -out "$TLS_DIR/server/cert.pem"

# OpenSSL (Python in cf-backup) finds a CA in SSL_CERT_DIR by its subject hash;
# Go (cf-terraforming, OpenTofu and the provider) reads every file there.
cp "$TLS_DIR/server/ca.pem" "$TLS_DIR/ca/$(openssl x509 -hash -noout -in "$TLS_DIR/server/ca.pem").0"

# cf-mock runs as nobody and cf-backup as uid 1000: both must read the files.
# The key is a throwaway for a container on a private network.
chmod 0755 "$TLS_DIR" "$TLS_DIR/server" "$TLS_DIR/ca"
chmod 0644 "$TLS_DIR"/server/* "$TLS_DIR"/ca/*

openssl verify -CAfile "$TLS_DIR/server/ca.pem" "$TLS_DIR/server/cert.pem" > /dev/null
echo "TLS material for cf-mock created in $TLS_DIR (CA $(openssl x509 -noout -fingerprint -sha256 -in "$TLS_DIR/server/ca.pem" | cut -d= -f2 | cut -c1-23)...)"
