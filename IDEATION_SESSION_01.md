# NewsFeed Ideation Session 01 (Agentic Architecture Vision)

## North Star
Build a **Telegram-native, fully configurable personal news intelligence system** where users control everything in plain language, while a multi-agent backend performs continuous research, debate, scoring, and editorial synthesis.

The experience should feel like a clean news assistant, but underneath it runs a **decisive intelligence engine** with memory, orchestration, caching, and quality review layers.

---

## Core System Dynamic (Your Vision, Formalized)

### Layer 0 — User Interface + Command Plane
- **Communication Agent (Telegram-facing)** receives user requests and delivers final reports.
- Accepts natural-language control over:
  - tone,
  - schedule,
  - format,
  - topic priorities,
  - source weightings,
  - verbosity,
  - number of items (default cap 10 unless user requests more).

### Layer 1 — Orchestration Brain (Top-Level Agent)
- **Orchestration Agent** is the central planner and message router.
- Translates user intent + profile memory into machine-readable research briefs.
- Assigns tasks to specialist research agents in parallel.
- Maintains lifecycle state for each request (queued -> researching -> expert review -> editorial review -> delivered).

### Layer 2 — Parallel Research Swarm (Standby Specialists)
Default standing team:
- **3 X/Twitter Agents** (trend + signal extraction).
- **3 Reddit Agents** (community sentiment + long-tail discovery).
- **5 News Source Agents** (one high-quality source per agent for efficiency; e.g., Guardian + other low-partisan outlets).
- **General Web Scraping Agents** (open-web discovery, optimized for breadth and speed).

Each research agent:
1. Executes source-specific crawl/search.
2. Produces an internal ranked research report.
3. Selects **Top 5 discoveries** aligned to orchestrator prompt + user weighting context provided by orchestrator.

### Layer 3 — Roundtable Expert Council
- Expert agents evaluate all candidate items for:
  - source quality,
  - research quality,
  - relevance to active user priorities,
  - likelihood of user affinity (experts have preference-memory access),
  - novelty and predictive value.
- Experts select the best items for user delivery:
  - default **max 10** per request,
  - additional ranked items retained in cache for fast “show me more” expansion.

### Layer 4 — Deep Report Drafting
- Experts co-author **extensive internal reports** for each selected item.
- Internal reports are intentionally more detailed than user-facing output.
- These reports become reusable structured artifacts for future updates/follow-ups.

### Layer 5 — Editorial Review Layer (Second-to-Top)
- **Two Review Agents** rewrite and optimize final output for reader preferences.
- They hold long-term memory for preferred voice/style constraints.
- Responsibilities:
  - consistency,
  - readability,
  - concise executive framing,
  - preserving factual integrity while tailoring style.

### Layer 6 — Delivery + Feedback Loop
- Communication Agent sends final polished report via Telegram.
- User replies in plain language.
- Orchestration Agent applies requested changes across system policies and agent instructions.

---

## Mandatory Memory Model

### Memory Domains
1. **Preference Memory**: topics, tone, depth, reading style, source likes/dislikes.
2. **Behavioral Memory**: click/open patterns, skips, “more/less like this,” follow-up requests.
3. **Session Memory**: current request constraints and transient directives.
4. **System Memory**: agent performance stats, source reliability history, routing optimizations.

### Memory Rules
- User can update behavior at any time through Telegram.
- System should apply preference changes globally and immediately.
- Bias handling is **fully user-controlled** (no forced balancing unless requested).
- Explanations are allowed (“why shown”), but never expose raw model internals.

---

## Prioritization and Caching Policy

### Prioritization Flow
1. Orchestrator dispatches weighted prompt.
2. Research agents return top-5 lists.
3. Experts score/merge and select top 10 default.
4. Editorial review layer prepares final delivery.

### Caching Requirements
- Non-selected but high-quality candidates are **never discarded immediately**.
- Cache by:
  - user,
  - topic cluster,
  - recency window,
  - confidence score.
- On “show me more,” return from cache first to avoid full re-run unless stale.

---

## Suggested Agent Registry (Initial)

### Core Control Agents
- `orchestrator_agent`
- `communication_agent`
- `system_optimization_agent` (global policy deployment + runtime tuning)

### Research Agents
- `x_agent_1`, `x_agent_2`, `x_agent_3`
- `reddit_agent_1`, `reddit_agent_2`, `reddit_agent_3`
- `news_agent_guardian`
- `news_agent_reuters` (or equivalent low-partisan source)
- `news_agent_ap` (or equivalent low-partisan source)
- `news_agent_bbc_world` (or equivalent)
- `news_agent_ft_world` (or equivalent)
- `web_scraper_agent_1`, `web_scraper_agent_2`, `web_scraper_agent_3`

### Quality + Editorial Agents
- `expert_quality_agent`
- `expert_relevance_agent`
- `expert_preference_fit_agent`
- `review_agent_style`
- `review_agent_clarity`

---

## Debate and Discussion Mechanics (Baked-In)

### Structured Roundtable Protocol
- Each expert submits:
  1. keep/drop recommendation,
  2. confidence,
  3. one-sentence rationale,
  4. risk note.
- Conflicts trigger short arbitration round before final selection.
- Final slate requires quality and relevance threshold pass.

### Anti-Noise Safeguards
- Duplicate-story collapse across sources.
- Low-information repetition filter.
- “No update” suppression unless meaningful change delta.

---

## User-Facing Output Contract (Per Delivery)
For each item shown:
1. Headline + source.
2. Why it matters to the user now.
3. What changed since prior cycle.
4. Predictive outlook (including markets as one signal, not dominant).
5. Optional deep links.

Default delivery size: **up to 10 items**.
User can request more; system expands from cached ranked reserve.

---

## Implementation Roadmap

### Phase 1 — Single-User Vertical Slice
- Telegram communication agent.
- Orchestrator + minimal research swarm.
- Expert triage + two-agent editorial review.
- Cache-enabled “show me more.”
- Basic memory updates from natural language.

### Phase 2 — Full Swarm + Robust Memory
- Expand to full agent registry.
- Strengthen preference and behavioral memory.
- Add system optimization agent for dynamic retuning.
- Add source-health and agent-performance observability.

### Phase 3 — Multi-User Productization
- Fast onboarding templates.
- User-exportable feed configurations.
- Shared profiles with individual overlays.

---

## Key Design Constraints to Preserve
1. The UI remains simple; complexity lives in the agent backend.
2. Prediction markets remain integrated but not overweighted.
3. User customization is universal and plain-language driven.
4. Reports are high quality by default and deeply tailorable.
5. Cached intelligence prevents unnecessary recomputation and latency.

---

## Final Product Positioning Statement
**NewsFeed is a personal news OS powered by an agentic research-and-debate engine: dozens of specialized agents discover, argue, verify, and refine intelligence so each user receives a fully customized Telegram briefing that gets smarter with every conversation.**

---

## V1 Execution Upgrade (Applied)
To move from ideation to implementation, V1 now uses a **config-first agent runtime**:
- Agent personas and mandates are declared in `config/agents.json`.
- Orchestration stages and limits are declared in `config/pipelines.json`.
- Runtime loads these configs so routing changes can be made without code rewrites.

This gives us a clean path to iterate quickly on agent behavior, ranking flow, and delivery style from configuration.
