#!/usr/bin/env bash
set -euo pipefail

echo "== Branches =="
git branch -vv

echo
if git remote get-url origin >/dev/null 2>&1; then
  echo "== Origin =="
  git remote -v
else
  echo "== Origin =="
  echo "origin is not configured"
fi

echo
current_branch=$(git branch --show-current)
echo "Current branch: ${current_branch}"

echo
if [ -n "$(git status --porcelain)" ]; then
  echo "Working tree is dirty"
  git status --short
else
  echo "Working tree is clean"
fi

echo
if git rev-parse --verify main >/dev/null 2>&1; then
  echo "main points to: $(git rev-parse --short main)"
else
  echo "main branch missing"
fi

if git rev-parse --verify work >/dev/null 2>&1; then
  echo "work points to: $(git rev-parse --short work)"
else
  echo "work branch missing"
fi
