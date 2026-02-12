# Merge Safe Bundle

This bundle exists to create a brand-new PR that **adds files only** and does not modify existing tracked files.

## Included conflict-prone files (snapshotted)
- `README.md`
- `docs/SYSTEM_ARCHITECTURE.md`
- `docs/V1_EXECUTION_PLAN.md`
- `src/newsfeed/agents/simulated.py`
- `src/newsfeed/models/config.py`
- `src/newsfeed/models/domain.py`
- `src/newsfeed/orchestration/bootstrap.py`
- `src/newsfeed/orchestration/engine.py`
- `tests/test_engine.py`

These are copied into `merge_safe_snapshot/` with the same relative paths.

## Purpose
- Avoid touching existing files in a new PR branch.
- Preserve current desired content in additive form.
- Enable manual adoption/cherry-pick without merge conflicts.
