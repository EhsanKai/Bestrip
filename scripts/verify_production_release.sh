#!/usr/bin/env bash
# Production release gate (V9 staging/production ops readiness).
#
# Why this exists: the root Dockerfile and CI's `image` job both run plain
# `npm run build`, deliberately - a staging build, a PR build, and every
# push to CI must stay buildable even while frontend/src/privacy/legalConfig.ts
# is still all placeholder values (see docs/V9_LIMITED_BETA_PRIVACY_UI_REPORT.md).
# Baking `npm run build:release` into the Dockerfile/CI would brick every one
# of those builds today, not just a real production one.
#
# The Vercel/Netlify split-hosting configs already call `build:release`
# (frontend/vercel.json, frontend/netlify.toml), so a split-hosting deploy is
# already gated. The single-origin Docker image - the "recommended" path per
# DEPLOY.md, and the one render.yaml deploys - has no equivalent gate: nothing
# stops that image from reaching production with the legal metadata still
# empty and COMPANION_TRAVELLER_LEGAL_NOTICE_APPROVED still false.
#
# This script is that gate. Run it deliberately, immediately before a real
# production release of the single-origin image - never as part of the
# generic build/CI path. It does not build or deploy anything itself.
#
# Usage:  scripts/verify_production_release.sh
# Exit 0 = the frontend legal configuration is production-shaped.
# Exit 1 = it is not; do not release.

set -uo pipefail
cd "$(dirname "$0")/.."

echo "=== Production legal readiness gate (frontend/src/privacy/legalConfig.ts) ==="
if ! (cd frontend && npm run --silent verify:legal); then
  echo
  echo "STOP: production legal configuration is not ready. Do not release."
  echo "This is a legal/Ops gate, not a code defect - see docs/V9_LIMITED_BETA_PRIVACY_UI_REPORT.md"
  echo "and docs/V9_STAGING_PRODUCTION_OPS_READINESS_REPORT.md before touching legalConfig.ts."
  exit 1
fi

echo
echo "OK: frontend legal configuration checks passed."
echo "This script does not and cannot verify: DETOURA_ENV=production, Secure cookies,"
echo "AUTH_TRUSTED_PROXY_HOPS, backup/restore, or any other runtime deployment setting -"
echo "see docs/V9_PRODUCTION_RELEASE_CHECKLIST.md for the full, manually-verified list."
