# Conflict Prevention and Mainline Workflow

## Why this kept happening
The recurring PR conflict pattern is caused by branch drift:
- feature work is created from an older base,
- `main` advances,
- the feature branch is not rebased/merged before PR creation,
- GitHub flags conflicts in shared files (`README.md`, docs, engine modules, tests).

In this repo, that drift is amplified by rapid iteration across the same core files.

## Required workflow (always)
1. Start from up-to-date `main`.
2. Create/refresh working branch from `main`.
3. Rebase working branch onto latest `main` before PR.
4. Run tests + compile checks.
5. Push and open PR.

## Local commands
```bash
# one-time
git fetch origin

# keep local main current
git checkout main
git pull --ff-only origin main

# refresh work branch
git checkout work
git rebase main

# validate
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=src python -m compileall -q src tests
```

## If conflicts occur
- Resolve conflicts locally (never in a rushed web edit for code-heavy files).
- Run tests again.
- Continue rebase:

```bash
git add <resolved-files>
git rebase --continue
```

## Final push
```bash
git push -u origin work
# or after rebase
git push --force-with-lease origin work
```

Use `--force-with-lease` only after intentional rebase.

## Pre-PR mergeability check
Run this before opening or updating a PR:

```bash
tools/check_merge_readiness.sh main work
```

Exit meanings:
- `0`: merge-ready (or rebase recommended but no textual conflicts),
- `3`: head branch is behind base branch,
- `4`: merge conflicts detected.

If conflict (`4`), resolve locally via rebase and re-run tests before pushing.
