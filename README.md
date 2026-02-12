# NewsFeed

NewsFeed is a configurable, Telegram-native news intelligence system powered by an agent swarm.

## V1 Goal
Ship a single-user vertical slice with:
- orchestrated multi-source research,
- expert roundtable ranking,
- editorial polishing,
- cache-backed "show me more",
- natural-language preference updates.

## Project Structure
- `config/agents.json`: declarative registry of agent roles and capabilities.
- `config/pipelines.json`: stage-by-stage processing graph and limits.
- `docs/V1_EXECUTION_PLAN.md`: build sequence, milestones, and acceptance criteria.
- `docs/SYSTEM_ARCHITECTURE.md`: architecture blueprint for runtime components.
- `src/newsfeed/`: runtime scaffold for orchestration, memory, and delivery.

## Quick start
```bash
PYTHONPATH=src python -m newsfeed.orchestration.bootstrap
```

## Tests
```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'
```
