# NewsFeed V1 Execution Plan

## What changed in this revision
This V1 plan is optimized for an **agent-config-first architecture**: behavior is declared in configuration and executed by a thin orchestration runtime. Routing, personas, limits, and quality controls can be tuned without rewriting core code.

## V1 System Objective
Deliver a daily and on-demand Telegram briefing where:
1. multiple source-specific agents research in parallel,
2. experts debate and rank candidate stories,
3. review agents adapt output style to reader preferences,
4. communication agent delivers polished output,
5. user instructions immediately update memory + future routing.

## Operating Constraints
- Default response cap: 10 items.
- Each selected item includes 2–3 adjacent reads.
- Prediction markets included as one signal (not dominant).
- Bias controls remain user-managed.
- Cached reserve is reused before re-crawling on “show me more”.

## Processing Flow (V1)
1. **Ingress** (Telegram message -> orchestration request).
2. **Brief compile** (apply user profile + session constraints).
3. **Parallel research swarm** (X, Reddit, source-news, web agents).
4. **Top-5 per research agent** candidate submission.
5. **Expert council** scoring + arbitration.
6. **Selection** top-10 (or requested count).
7. **Internal deep report** drafting.
8. **Two-agent editorial optimization** for tone + clarity.
9. **Delivery** through communication agent.
10. **Feedback writeback** to preference memory + cache.

## Milestones
### M1 — Runtime and config foundation
- [x] Load/validate agent and pipeline configuration.
- [x] Registry resolution for enabled agents by stage.
- [x] Request envelope model.

### M2 — Research and ranking loop
- [x] Fan-out API for research agents (simulated deterministic implementation).
- [x] Candidate normalization and dedupe.
- [x] Expert scoring contract + arbitration rule (single pass selection in council).

### M3 — Report generation and delivery
- [x] Internal report data model.
- [x] Editorial review pass scaffold (formatter and payload contract).
- [ ] Telegram API integration.

### M4 — Memory and cache integration
- [x] Preference memory updates from natural language controls (weight/style mutators).
- [x] Reserved-candidate cache with freshness policy.
- [x] “Show me more” retrieval path.

## Acceptance Criteria
- Routing graph can be modified from JSON config only.
- End-to-end dry run produces a valid ranked slate and formatted payload.
- A preference change command changes the next request brief.
- Repeated “more” requests hit cache before full re-run when fresh.

## Next technical priorities
1. Replace simulated research agents with real source adapters.
2. Add asynchronous execution per research fanout stage.
3. Wire Telegram send/edit/retry adapter with durable delivery logs.
4. Persist profile/cache stores in SQLite or Postgres.
