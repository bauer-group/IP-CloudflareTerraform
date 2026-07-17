# =============================================================================
#  CloudflareTerraform · cloudflare-backup — meta image on the BackupHelper engine
# -----------------------------------------------------------------------------
#  A dual-mode image on the central BackupHelper engine
#  (ghcr.io/bauer-group/cs-backuphelper/backuphelper): it adds the `tofu` +
#  `cf-terraforming` binaries and a pip plugin that registers BOTH the
#  `cloudflare` backup source AND the `cloudflare` operator command group
#  (diff / apply / drift / export).
#
#    • Backup mode (scheduler daemon, no args): the engine runs the `cloudflare`
#      source on cron → deterministic tar.gz + sha256 manifest → off-site S3 →
#      retention (age_days + GFS) → alerting. All owned by the engine.
#    • CLI mode: `docker compose run --rm cf-backup <verb>` — engine verbs
#      (--now/list/verify/restore/prune) plus `cloudflare diff|apply|drift|export`.
#
#  cf-terraforming drives OpenTofu via --terraform-binary-path and reads the
#  provider schema from an initialized working dir; both binaries live here so
#  they share that dir. No official cf-terraforming image exists, and the
#  OpenTofu image is FROM scratch — so the tofu binary is lifted with a
#  multi-stage COPY (which does NOT trigger the image's ONBUILD guard).
#
#  Build:   docker build -t cloudflare-backup src/cloudflare-backup
#  Source:  https://github.com/bauer-group/IP-CloudflareTerraform
# =============================================================================

# OpenTofu >= 1.11 is required for provider-v5 write-only attributes.
ARG TOFU_VERSION=1.12.4
FROM ghcr.io/opentofu/opentofu:${TOFU_VERSION}-minimal AS tofu

FROM ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest

# Release version injected at build time (--build-arg IMAGE_VERSION); the
# default is kept in sync with the latest release by the sync-dockerfile-
# versions job in docker-release.yml.
ARG IMAGE_VERSION=0.1.1
# cf-terraforming >= 0.24.0 is required for Cloudflare provider v5 (0.27.0 latest).
ARG CF_TERRAFORMING_VERSION=0.27.0
# Provider pin used to pre-warm the plugin cache (matches the source default).
ARG CLOUDFLARE_PROVIDER_VERSION=">= 5.8.2, < 6.0.0"

# ---------------------------------------------------------------------------
# Binaries: tofu (multi-stage copy) + cf-terraforming (pinned release). Running
# `version` on both here doubles as a build-time musl smoke test — a binary that
# does not run on Alpine fails the build.
# ---------------------------------------------------------------------------
USER root
COPY --from=tofu /usr/local/bin/tofu /usr/local/bin/tofu
RUN set -eux; \
    apk add --no-cache --virtual .dl curl; \
    curl -fsSL "https://github.com/cloudflare/cf-terraforming/releases/download/v${CF_TERRAFORMING_VERSION}/cf-terraforming_${CF_TERRAFORMING_VERSION}_linux_amd64.tar.gz" \
      | tar -xz -C /usr/local/bin cf-terraforming; \
    chmod +x /usr/local/bin/tofu /usr/local/bin/cf-terraforming; \
    apk del .dl; \
    /usr/local/bin/tofu version; \
    /usr/local/bin/cf-terraforming version

# ---------------------------------------------------------------------------
# Plugin: install the package (registers the backuphelper.sources +
# backuphelper.commands entry points) and run its pytest gate — a red suite
# fails the build.
# ---------------------------------------------------------------------------
COPY pyproject.toml README.md /opt/cloudflare-backup/
COPY backuphelper_cloudflare/ /opt/cloudflare-backup/backuphelper_cloudflare/
COPY tests/ /opt/cloudflare-backup/tests/
RUN set -eux; \
    pip install --no-cache-dir "/opt/cloudflare-backup[test]"; \
    python -m pytest -q /opt/cloudflare-backup/tests; \
    pip uninstall -y pytest >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# Best-effort provider plugin cache so scheduled backups do not re-download the
# Cloudflare provider on every run (and stay off the registry rate limit).
# Non-fatal: a network-less build still produces a working image that downloads
# the provider on first use.
# ---------------------------------------------------------------------------
ENV TF_PLUGIN_CACHE_DIR=/opt/tofu-plugin-cache
RUN set -eux; \
    mkdir -p "${TF_PLUGIN_CACHE_DIR}" /tmp/warm; \
    printf 'terraform {\n  required_providers {\n    cloudflare = {\n      source = "cloudflare/cloudflare"\n      version = "%s"\n    }\n  }\n}\n' "${CLOUDFLARE_PROVIDER_VERSION}" > /tmp/warm/main.tf; \
    (cd /tmp/warm && tofu init -input=false -backend=false >/dev/null 2>&1) || echo "warn: provider pre-cache skipped (no network at build)"; \
    rm -rf /tmp/warm; \
    # The non-root `backup` user (uid 1000) must be able to write the cache lock
    # file on `tofu init` — own the cache, don't leave it read-only.
    chown -R 1000:1000 "${TF_PLUGIN_CACHE_DIR}"; \
    chmod -R u+rwX "${TF_PLUGIN_CACHE_DIR}"

USER backup

# ---------------------------------------------------------------------------
# Metadata Labels (Legacy)
# ---------------------------------------------------------------------------
LABEL vendor="BAUER GROUP"
LABEL maintainer="Karl Bauer <kb@de.bauer-group.com>"

# ---------------------------------------------------------------------------
# OCI Image Spec Labels
# https://github.com/opencontainers/image-spec/blob/main/annotations.md
# ---------------------------------------------------------------------------
LABEL org.opencontainers.image.title="CloudflareTerraform Cloudflare-Backup"
LABEL org.opencontainers.image.description="Scheduled cf-terraforming export of a Cloudflare account to OpenTofu HCL with retention, off-site S3, alerting, plus diff/apply/drift CLI verbs - BAUER GROUP Edition"
LABEL org.opencontainers.image.vendor="BAUER GROUP"
LABEL org.opencontainers.image.authors="Karl Bauer <kb@de.bauer-group.com>"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/bauer-group/IP-CloudflareTerraform"
LABEL org.opencontainers.image.url="https://github.com/bauer-group/IP-CloudflareTerraform"
LABEL org.opencontainers.image.documentation="https://github.com/bauer-group/IP-CloudflareTerraform#readme"
LABEL org.opencontainers.image.base.name="ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest"
LABEL org.opencontainers.image.version="${IMAGE_VERSION}"

# Inherit ENTRYPOINT, USER, ENV, HEALTHCHECK from the base image.
# Sources / destinations / schedule / retention come from BACKUP_CONFIG_JSON.
#
# Usage:
#   docker compose up -d cf-backup          # backup mode (daemon)
#   docker compose run --rm cf-backup --now # ad-hoc backup
#   docker compose run --rm cf-backup cloudflare diff <a> <b>
#   docker compose run --rm cf-backup cloudflare apply <id> --zone <z>
#   docker compose run --rm cf-backup cloudflare drift
