# NewsFeed System Architecture (V1 Foundation)

## Overview
This document defines the first runnable architecture for NewsFeed's agentic research pipeline.

## Runtime Pattern
- **Config-first**: roles, mandates, and stage flow come from config files.
- **Thin engine**: orchestration code executes the graph and composes outputs.
- **Replaceable agents**: simulated agents provide deterministic behavior now and can be swapped with API-backed implementations later.

## Core Runtime Components
1. `PreferenceStore`: persistent user-level style/topic controls.
2. `CandidateCache`: reserve pool for fast "show me more" responses.
3. `SimulatedResearchAgent`: source-specific candidate generation.
4. `ExpertCouncil`: score merge, dedupe, and selection.
5. `TelegramFormatter`: final user-facing report rendering.
6. `NewsFeedEngine`: request lifecycle coordinator.

## Request Lifecycle
1. Orchestrator receives prompt + user/topic weights.
2. Research agents each generate top-K candidates.
3. Expert council picks top-N for delivery and stores reserve.
4. Report items are synthesized with why/changed/outlook fields.
5. Telegram formatter produces final text payload.
6. Cache can return additional ranked results on demand.

## Immediate Next Build Targets
- Add real source adapters (X/Reddit/Guardian/Polymarket/Kalshi).
- Persist memory and cache to durable storage.
- Add explicit debate protocol output objects.
- Add asynchronous fanout for real network workloads.
