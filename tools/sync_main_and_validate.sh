#!/usr/bin/env bash
set -euo pipefail

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "origin remote is not configured."
  echo "Add it first: git remote add origin <repo-url>"
  exit 2
fi

current_branch=$(git branch --show-current)

echo "Fetching origin..."
git fetch origin

echo "Updating main..."
git checkout main
git pull --ff-only origin main

echo "Rebasing current branch (${current_branch}) onto main..."
git checkout "${current_branch}"
if [ "${current_branch}" != "main" ]; then
  git rebase main
fi

echo "Running validation..."
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=src python -m compileall -q src tests

echo "Done. Branch is synced with main and validated."
