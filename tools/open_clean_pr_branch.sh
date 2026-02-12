#!/usr/bin/env bash
set -euo pipefail

base_branch="${1:-main}"
new_branch="${2:-pr/refresh-newsfeed}"

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "origin remote is not configured."
  echo "Add it first: git remote add origin <repo-url>"
  exit 2
fi

if ! git rev-parse --verify "${base_branch}" >/dev/null 2>&1; then
  echo "Base branch not found: ${base_branch}" >&2
  exit 2
fi

echo "Fetching origin..."
git fetch origin

echo "Refreshing local ${base_branch} from origin/${base_branch}..."
git checkout "${base_branch}"
git pull --ff-only origin "${base_branch}"

if git rev-parse --verify "${new_branch}" >/dev/null 2>&1; then
  echo "Branch ${new_branch} already exists locally; resetting to ${base_branch}."
  git checkout "${new_branch}"
  git reset --hard "${base_branch}"
else
  echo "Creating ${new_branch} from ${base_branch}."
  git checkout -b "${new_branch}" "${base_branch}"
fi

echo "Running merge-readiness and validation..."
tools/check_merge_readiness.sh "${base_branch}" "${new_branch}"
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=src python -m compileall -q src tests

echo "Pushing ${new_branch} and setting upstream..."
git push -u origin "${new_branch}"

echo "Done. Open a new PR from ${new_branch} -> ${base_branch} and close stale draft PRs."
