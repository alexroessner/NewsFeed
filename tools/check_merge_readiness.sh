#!/usr/bin/env bash
set -euo pipefail

base_branch="${1:-main}"
head_branch="${2:-work}"

if ! git rev-parse --verify "${base_branch}" >/dev/null 2>&1; then
  echo "Base branch not found: ${base_branch}" >&2
  exit 2
fi
if ! git rev-parse --verify "${head_branch}" >/dev/null 2>&1; then
  echo "Head branch not found: ${head_branch}" >&2
  exit 2
fi

base_sha=$(git rev-parse "${base_branch}")
head_sha=$(git rev-parse "${head_branch}")

if [ "${base_sha}" = "${head_sha}" ]; then
  echo "${head_branch} and ${base_branch} point to the same commit (merge-ready)."
  exit 0
fi

if git merge-base --is-ancestor "${base_sha}" "${head_sha}"; then
  echo "${head_branch} is ahead of ${base_branch} with a clean ancestry path."
  exit 0
fi

if git merge-base --is-ancestor "${head_sha}" "${base_sha}"; then
  echo "${head_branch} is behind ${base_branch}; rebase/merge required before PR."
  exit 3
fi

if git merge-tree "$(git merge-base "${base_sha}" "${head_sha}")" "${base_sha}" "${head_sha}" | rg -q "^<<<<<<< "; then
  echo "Merge conflicts detected between ${base_branch} and ${head_branch}."
  exit 4
fi

echo "Branches have diverged but merge-tree found no textual conflicts. Rebase recommended."
exit 0
