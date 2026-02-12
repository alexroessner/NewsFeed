# NewsFeed

NewsFeed is a configurable, Telegram-native news intelligence system powered by an agent swarm.

## V1 Goal
Ship a single-user vertical slice with:
- orchestrated multi-source research,
- expert roundtable ranking,
- editorial polishing,
- cache-backed "show me more",
- natural-language preference updates,
- persona-driven review lenses.

## Project Structure
- `config/agents.json`: declarative registry of agent roles and capabilities.
- `config/pipelines.json`: stage-by-stage processing graph and limits.
- `config/review_personas.json`: active editorial/review personas and notes.
- `personas/*.md`: cognitive stance prompts inspired by persona-driven AI workflows.
- `docs/V1_EXECUTION_PLAN.md`: build sequence, milestones, and acceptance criteria.
- `docs/SYSTEM_ARCHITECTURE.md`: architecture blueprint for runtime components.
- `docs/REPO_RECOVERY_AND_RELEASE.md`: git recovery and release steps for blocked PR scenarios.
- `tools/git_health_check.sh`: local repo integrity checks before push.
- `src/newsfeed/`: runtime scaffold for orchestration, memory, review, and delivery.
- `docs/CONFLICT_PREVENTION.md`: branch/rebase workflow to avoid unmergeable PRs.
- `tools/sync_main_and_validate.sh`: one-command branch sync + validation helper.
- `tools/check_merge_readiness.sh`: detects behind/diverged/conflict state before opening PR.
- `tools/open_clean_pr_branch.sh`: creates a fresh PR branch from latest `main` and validates before push.
- `tools/bootstrap_github_remote.sh`: configures `origin` from env secrets (`GH_TOKEN`/`GITHUB_TOKEN` + repo slug).

## Quick start
```bash
PYTHONPATH=src python -m newsfeed.orchestration.bootstrap
```

## Tests
```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=src python -m compileall -q src tests
```
