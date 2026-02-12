#!/usr/bin/env bash
set -euo pipefail

# Bootstrap origin remote for non-interactive environments.
# Requires one token env var and one repo env var:
# - token: GH_TOKEN or GITHUB_TOKEN
# - repo:  GH_REPO (owner/repo), GITHUB_REPOSITORY (owner/repo), or ORIGIN_REPO

TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
REPO_SLUG="${GH_REPO:-${GITHUB_REPOSITORY:-${ORIGIN_REPO:-}}}"

if [ -z "${TOKEN}" ]; then
  echo "No GitHub token found. Set GH_TOKEN or GITHUB_TOKEN."
  exit 2
fi

if [ -z "${REPO_SLUG}" ]; then
  echo "No repository slug found. Set GH_REPO or GITHUB_REPOSITORY (owner/repo)."
  exit 2
fi

if ! printf '%s' "${REPO_SLUG}" | rg -q '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'; then
  echo "Invalid repo slug format: ${REPO_SLUG} (expected owner/repo)"
  exit 2
fi

REMOTE_URL="https://${TOKEN}@github.com/${REPO_SLUG}.git"

if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "${REMOTE_URL}"
  echo "Updated origin remote for ${REPO_SLUG}."
else
  git remote add origin "${REMOTE_URL}"
  echo "Added origin remote for ${REPO_SLUG}."
fi

# Validate credentials/reachability without printing token.
if git ls-remote --heads origin >/dev/null 2>&1; then
  echo "Origin connectivity check passed."
else
  echo "Origin connectivity check failed. Verify token scope/repo access/network."
  exit 3
fi
