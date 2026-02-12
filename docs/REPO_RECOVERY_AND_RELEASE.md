# Repo Recovery and Release Playbook

This project had a failed/blocked PR merge path. This playbook ensures work is preserved and safely moved to GitHub.

## 1) Preserve local state first
From repo root:

```bash
git branch -f main HEAD
git tag -f recovery-$(date +%Y%m%d-%H%M%S) HEAD
```

This guarantees the current work is anchored on `main` and a timestamped tag.

## 2) Verify repo health

```bash
tools/git_health_check.sh
```

Expected:
- clean working tree,
- `main` present,
- `work` present (optional),
- `origin` either configured or clearly absent.

## 3) Configure remote (if missing)

```bash
git remote add origin <YOUR_GITHUB_REPO_URL>
# or
# git remote set-url origin <YOUR_GITHUB_REPO_URL>
```

## 4) Push safely

### Preferred (fast-forward)
```bash
git checkout main
git push -u origin main
```

### If remote diverged and you intentionally want local to win
```bash
git checkout main
git push --force-with-lease origin main
```

Use `--force-with-lease` (not raw `--force`) to reduce accidental overwrite risk.

## 5) PR strategy
If an old PR is unmergeable:
1. Close stale PR.
2. Create a new branch from `main` if needed.
3. Open a fresh PR with the full working tree.

## 6) Ongoing workflow recommendations
- Keep feature work on `work` (or short-lived feature branches).
- Rebase/merge into `main` only after tests pass.
- Run CI before push (see `.github/workflows/ci.yml`).

## 7) If a draft PR stays unmergeable
Use a fresh branch from updated `main` and avoid stacking old conflict history:

```bash
git checkout main
git pull --ff-only origin main
git checkout -b pr/refresh-newsfeed
# if needed: cherry-pick specific commits from old branch
# git cherry-pick <sha1> <sha2> ...

# validate
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=src python -m compileall -q src tests

# push
git push -u origin pr/refresh-newsfeed
```

Then open a **new PR** and close the stale draft PR.
