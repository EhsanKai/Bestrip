#!/usr/bin/env bash
# Staging noindex contract verification (V9 staging hardening).
#
# Why this exists: a real Render deployment shipped with DETOURA_ENV=production
# (needed for Secure cookies) and no VITE_STAGING build arg, which silently
# made it search-indexable - the exact opposite of what "staging" is supposed
# to mean. DETOURA_FORCE_NOINDEX (backend) and VITE_STAGING (frontend build
# arg) together are the fix; this script is what catches a future deployment
# that forgets one half of it. It makes no requests other than plain GETs and
# changes nothing.
#
# Usage:  scripts/verify_staging_noindex.sh <base-url>
# Example: scripts/verify_staging_noindex.sh https://detoura-iug3.onrender.com
#
# Exit 0 = both halves of the staging noindex contract are live.
# Exit 1 = at least one is missing; do not treat this deployment as staging.

set -uo pipefail

base_url="${1:?Usage: $0 <base-url>, e.g. https://detoura-iug3.onrender.com}"
base_url="${base_url%/}"
failed=0

echo "=== Staging noindex contract: ${base_url} ==="

echo
echo "--- Backend: X-Robots-Tag response header ---"
robots_header=$(curl -fsSI "${base_url}/" | grep -i "^x-robots-tag:" || true)
if [[ "${robots_header}" == *"noindex"* ]]; then
  echo "OK: ${robots_header}"
else
  echo "MISSING: no 'X-Robots-Tag: noindex' header on / - DETOURA_FORCE_NOINDEX is not active."
  failed=1
fi

echo
echo "--- Frontend: <meta name=robots> tag ---"
meta_tag=$(curl -fsS "${base_url}/" | grep -o '<meta name="robots"[^>]*>' || true)
if [[ "${meta_tag}" == *"noindex"* ]]; then
  echo "OK: ${meta_tag}"
else
  echo "MISSING or indexable: '${meta_tag:-<none found>}' - VITE_STAGING=true did not reach the frontend build."
  failed=1
fi

echo
echo "--- Frontend: robots.txt ---"
robots_txt=$(curl -fsS "${base_url}/robots.txt" || true)
if echo "${robots_txt}" | grep -qE "^Disallow: /[[:space:]]*$"; then
  echo "OK: blanket Disallow: / present."
else
  echo "MISSING: robots.txt does not blanket-disallow - VITE_STAGING=true did not reach the frontend build."
  echo "${robots_txt}"
  failed=1
fi

echo
if [[ "${failed}" -eq 0 ]]; then
  echo "PASS: this deployment is correctly non-indexable."
else
  echo "FAIL: this deployment is indexable (or partially so). See docs/V9_STAGING_RUNBOOK.md"
  echo "and render.yaml's DETOURA_FORCE_NOINDEX/VITE_STAGING comments."
fi
exit "${failed}"
