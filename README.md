# NewsFeed

Agentic Telegram news intelligence system powered by a multi-agent research swarm, expert council, and editorial review pipeline.

## What it does

NewsFeed deploys 23 research agents across 17 sources (BBC, Reuters, AP, Guardian, FT, Al Jazeera, NPR, CNBC, France 24, TechCrunch, Nature, HackerNews, arXiv, GDELT, Reddit, X/Twitter, Google News), runs candidates through a 7-stage intelligence pipeline (credibility, corroboration, urgency, diversity, clustering, geo-risk, trends), filters via a 5-expert council with weighted debate, applies editorial review (tone/style + clarity), and delivers personalized briefings via Telegram.

## Install

```bash
pip install -e .                    # Core (stdlib only)
pip install -e ".[all]"             # + LLM + Telegram support
pip install -e ".[dev]"             # + pytest + ruff
```

## Quick start

```bash
# 1. Set up API keys (all optional — agents without keys use simulated data)
cp config/secrets.json.example config/secrets.json
# Edit config/secrets.json with your API keys

# 2. Demo mode (no API keys needed)
python -m newsfeed.orchestration.bootstrap

# 3. Telegram bot mode (requires telegram_bot_token in config/secrets.json)
python -m newsfeed.orchestration.bootstrap
```

## API keys

API keys go in `config/secrets.json` (gitignored). Copy `config/secrets.json.example` to get started. Keys can also be set in `config/pipelines.json` under `api_keys`, or via environment variables in CI/CD.

All keys are optional — agents without keys fall back to simulated data. Free agents (BBC, HackerNews, Al Jazeera, arXiv, GDELT, Google News) work without any keys.

## Access control

Set `TELEGRAM_OWNER_ID` env var to your Telegram user ID for admin access. Configure `access_control` in `config/pipelines.json`:

```json
{
  "access_control": {
    "owner_user_id": "YOUR_TELEGRAM_ID",
    "allowed_users": [],
    "open_registration": false
  }
}
```

## Tests

```bash
python -m pytest tests/ -v          # 779+ tests
```

## Architecture

```
User ← Telegram Bot ← Communication Agent
                            ↓
                     Orchestrator Agent
                            ↓
              ┌─────────────┼─────────────┐
              ↓             ↓             ↓
         23 Research    7 Intelligence   System
           Agents         Stages       Optimizer
              ↓             ↓
         5 Expert Council (weighted debate)
              ↓
         2 Editorial Review Agents
              ↓
         Formatted Briefing → Delivery
```

**Layers:**
- **Layer 0 — Communication:** Telegram bot + CommunicationAgent (message relay, commands, scheduling)
- **Layer 1 — Orchestration:** OrchestratorAgent (brief compilation, lifecycle, routing)
- **Layer 2 — Research:** 23 agents across 17 source types (async fan-out)
- **Layer 3 — Intelligence:** Credibility, corroboration, urgency, diversity, clustering, geo-risk, trends
- **Layer 4 — Expert Council:** 5 experts with heuristic + LLM voting, arbitration, DebateChair influence tracking
- **Layer 5 — Editorial:** StyleReviewAgent (tone/voice) + ClarityReviewAgent (concision/actionability)
- **Cross-cutting:** SystemOptimizationAgent (self-tuning), SystemConfigurator (plain-text config), AuditTrail (full decision tracking)

## Plain-text configuration

Users can modify any system parameter via natural language:

```
set evidence weight to 0.4       # Scoring weights
make experts stricter             # Expert council tuning
disable clustering                # Toggle pipeline stages
prioritize reuters over reddit    # Source priority
add persona forecaster            # Persona management
show me 15 items                  # Delivery preferences
```

## Project structure

```
config/
  agents.json              # 23 research + 5 expert + 3 control + 2 review agents
  pipelines.json           # 12 stages, scoring, intelligence, editorial review, API keys
  review_personas.json     # 4 editorial personas with cognitive stance notes
personas/                  # Persona prompt files (engineer, source_critic, audience, forecaster)
src/newsfeed/
  agents/                  # Research agents (BBC, Reuters, Guardian, Reddit, X, arXiv, GDELT, etc.)
  delivery/                # Telegram bot, formatter, scheduler
  intelligence/            # Credibility, clustering, urgency, geo-risk, trends
  memory/                  # Preferences, cache, state persistence, command parsing
  models/                  # Domain models (CandidateItem, ReportItem, etc.) + config loading
  orchestration/           # Engine, orchestrator, communication, optimizer, configurator, audit
  review/                  # Style + clarity review agents, persona stack
tests/                     # 779+ tests across all components
docs/                      # Architecture docs, execution plan, vision
```
