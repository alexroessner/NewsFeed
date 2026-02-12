# NewsFeed System Architecture (V1 Foundation)

## Overview
This document defines the first runnable architecture for NewsFeed's agentic research pipeline.

## Runtime Pattern
- **Config-first**: roles, mandates, and stage flow come from config files.
- **Thin engine**: orchestration code executes the graph and composes outputs.
- **Replaceable agents**: simulated agents provide deterministic behavior now and can be swapped with API-backed implementations later.
- **Persona-driven review**: editorial behavior is shaped by explicit persona prompts in `personas/`.

## Core Runtime Components
1. `PreferenceStore`: user-level style/topic controls.
2. `CandidateCache`: reserve pool for fast "show me more" responses.
3. `SimulatedResearchAgent`: source-specific candidate generation.
4. `ExpertCouncil`: debate votes + score merge, dedupe, and selection.
5. `PersonaReviewStack`: applies configured cognitive lenses to report framing.
6. `TelegramFormatter`: final user-facing report rendering.
7. `NewsFeedEngine`: request lifecycle coordinator with async research fan-out.

## Request Lifecycle
1. Orchestrator receives prompt + user/topic weights.
2. Research agents run asynchronously and each produce top-K candidates.
3. Expert council emits debate votes and picks top-N for delivery.
4. Reserve candidates are cached by user/topic for future expansion.
5. Persona review stack rewrites "why" and outlook framing.
6. Telegram formatter produces final text payload.

## Persona Workflow
Persona files live in `personas/` and are intentionally prompt-like, not implementation docs. They encode cognitive stance:
- `engineer.md`
- `source_critic.md`
- `audience.md`
- `forecaster.md`

The active set is controlled via `config/review_personas.json` and loaded at runtime.

## Immediate Next Build Targets
- Add real source adapters (X/Reddit/Guardian/Polymarket/Kalshi).
- Persist memory and cache to durable storage.
- Add full roundtable arbitration traces to delivery metadata.
- Add stage-level observability metrics and logging.
- Add style persona variants that users can switch through Telegram commands.
