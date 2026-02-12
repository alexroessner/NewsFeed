#!/usr/bin/env bash
set -euo pipefail

# Repairs a stale/conflicted PR branch by aligning it to latest main.
# This is safe for the "main is source of truth" workflow.
#
# Usage:
#   tools/repair_pr_branch.sh <pr-branch> [base-branch]
# Example:
#   tools/repair_pr_branch.sh work main

PR_BRANCH="${1:-}"
BASE_BRANCH="${2:-main}"

if [ -z "${PR_BRANCH}" ]; then
  echo "Usage: $0 <pr-branch> [base-branch]" >&2
  exit 2
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "origin remote is not configured."
  echo "Configure origin first (see tools/bootstrap_github_remote.sh)."
  exit 2
fi

if ! git rev-parse --verify "${BASE_BRANCH}" >/dev/null 2>&1; then
  echo "Base branch not found locally: ${BASE_BRANCH}" >&2
  exit 2
fi

echo "Fetching origin..."
git fetch origin

echo "Refreshing local ${BASE_BRANCH}..."
git checkout "${BASE_BRANCH}"
git pull --ff-only origin "${BASE_BRANCH}"

if ! git rev-parse --verify "origin/${PR_BRANCH}" >/dev/null 2>&1; then
  echo "Remote PR branch not found: origin/${PR_BRANCH}" >&2
  exit 2
fi

# Preserve conflicted branch state before rewriting.
backup_tag="backup/${PR_BRANCH}/$(date +%Y%m%d-%H%M%S)"
old_pr_sha="$(git rev-parse "origin/${PR_BRANCH}")"
git tag -f "${backup_tag}" "${old_pr_sha}"
echo "Backup tag created: ${backup_tag} -> ${old_pr_sha}"

echo "Checking out local ${PR_BRANCH} from origin/${PR_BRANCH}..."
git checkout -B "${PR_BRANCH}" "origin/${PR_BRANCH}"

echo "Hard-resetting ${PR_BRANCH} to ${BASE_BRANCH} to eliminate stale conflicts..."
git reset --hard "${BASE_BRANCH}"

echo "Pushing repaired ${PR_BRANCH} with force-with-lease..."
git push --force-with-lease origin "${PR_BRANCH}"

echo "Done. PR branch '${PR_BRANCH}' now matches '${BASE_BRANCH}' and should be mergeable."
