# NewsFeed: A Human User's Review

*If I were a person who needed this system, here's what I'd think.*

---

## What This System Actually Is

NewsFeed is a personal intelligence analyst that lives in your Telegram. You tell it what you care about — geopolitics, AI policy, markets — and it monitors 18+ news sources, runs stories through a credibility/urgency/relevance pipeline, has five "expert" agents debate what matters, applies editorial review lenses, and delivers a curated briefing. You can tune it with natural language ("more geopolitics, less crypto") and it remembers your preferences.

That's a genuinely ambitious and well-executed idea.

---

## What I'd Love About It (The Strengths)

### 1. The intelligence framing is right

Most news apps treat all stories equally. NewsFeed doesn't — it assigns confidence bands, tracks corroboration across sources, flags contrarian signals, and tells you *why* something matters and *what to watch next*. That's the difference between "here's what happened" and "here's what you should think about." For anyone making decisions based on news — investors, analysts, policy people — this framing is exactly right.

### 2. The expert council is a genuinely novel idea

Having five independent "experts" (quality, relevance, preference-fit, geopolitics, market-signal) vote on each story, with a debate chair tracking influence and arbitrating ties, is architecturally clever. It means no single scoring dimension dominates. A story with weak evidence but high geopolitical relevance still gets heard. This is better than a single composite score.

### 3. Configuration-first architecture is smart

Everything meaningful is in JSON config files — scoring weights, source tiers, urgency keywords, expert thresholds, editorial tone templates. A non-programmer could understand and tune `pipelines.json`. This means the system can evolve without code changes, which is critical for a system that learns what works.

### 4. The command surface is rich but learnable

36 commands sounds overwhelming, but the hierarchy makes sense:
- Core loop: `/briefing`, `/quick`, `/deep_dive`, `/more`
- Tuning: `/feedback`, `/settings`, `/topics`
- Analysis: `/sitrep`, `/compare`, `/entities`, `/diff`, `/timeline`
- Management: `/save`, `/tracked`, `/export`, `/preset`

A new user only needs `/briefing` and `/feedback`. Everything else is there when you're ready.

### 5. Source credibility tracking is genuinely useful

The tiered source system (Reuters/AP/BBC at 0.85 reliability, Reddit/X at 0.55) with bias profiles ("center", "left-leaning", "global-south-perspective") and corroboration bonuses (+0.08 per confirming source, capped at 0.20) means the system is epistemically honest about where information comes from. The `/sources` command showing a reliability dashboard is something I'd actually use.

### 6. Security posture is thoughtful

Unicode control character stripping on inputs, URL scheme validation (only http/https/ftp), HTML escaping throughout Telegram output, validated state restoration with clamped values on every field, admin-only system config changes, rate limiting, bounded user dicts to prevent memory exhaustion — this wasn't bolted on as an afterthought.

---

## What I'd Struggle With (The Friction Points)

### 1. The cold start problem

When I type `/start`, I get a welcome message and... then what? The system has 22 research agents and 7 intelligence stages, but it doesn't know *anything* about me yet. My first briefing will use the default topic weights (geopolitics 0.8, AI policy 0.7, technology 0.6, markets 0.5). What if I'm a biotech researcher? A climate journalist? A commodities trader?

**What I'd want:** An onboarding flow. Three quick questions in the first interaction:
- "What topics matter most to you?" (pick 3-5 from a list)
- "What's your role?" (investor / analyst / journalist / general interest / policy)
- "How detailed do you want your briefings?" (headlines only / standard / deep analysis)

This would seed topic weights, tone, and format in one 30-second interaction instead of requiring the user to discover and use `/feedback` commands iteratively.

### 2. The "why" and "outlook" feel templated

In `_assemble_report`, every story gets the same boilerplate:
```python
why = "Aligned with your weighted interest in {topic} and strong source quality."
outlook = "Market and narrative signals suggest elevated watch priority."
what_changed = "New cross-source confirmation and discussion momentum since last cycle."
```

These run through the style/clarity review agents (which can rewrite them via LLM), but when the LLM isn't connected (no API key), every story has the same generic framing. A user would notice this by the second briefing and stop reading those sections.

**What I'd want:** Even without an LLM, the system has enough metadata to generate more specific text. The candidate has a topic, source tier, urgency level, corroboration count, and regions. A simple template system could produce:
- "This Reuters report on EU AI regulation is corroborated by 3 other sources and marks an escalation from last week's developments."

vs.

- "Aligned with your weighted interest in ai_policy and strong source quality."

The data is all there — it just needs to reach the text.

### 3. Adjacent reads are placeholder text

Every story gets adjacent reads like `["Context read 1 for geopolitics", "Context read 2 for geopolitics", "Context read 3 for geopolitics"]`. These are generated in `_assemble_report` as string templates. In the formatted output, this shows up as:

> **Related:** Context read 1 for geopolitics . Context read 2 for geopolitics . Context read 3 for geopolitics

This is noise, not signal. A user seeing this would lose trust in the system's intelligence claims.

**What I'd want:** Either populate these from the actual reserve candidates in the same topic cluster (the data is in `self.cache`), or don't show them at all until they contain real content. Showing placeholder text is worse than showing nothing.

### 4. Telegram is both the strength and the ceiling

Telegram is a great initial delivery channel — it's where power users already live, it supports rich HTML formatting, and the bot API is solid. But the 4096-character message limit forces aggressive truncation, and multi-message briefings lose threading context. A 10-story briefing becomes 12+ separate messages (header + 10 cards + footer), which floods the chat.

The multi-message approach (header, individual story cards, closing) partially solves this, but Telegram doesn't have native "collapse" or "expand" functionality. Every briefing takes up a full screen of scrollback.

**What I'd want:** Two things:
1. A `/quick` mode that's truly quick — one message, 10 headlines, no analysis. (This exists! But it still includes one-liner context for each story, which makes it 2-3 screens.)
2. A web companion view where the full briefing lives as a single page, with the Telegram message being a summary + link. The `/export` Markdown feature hints at this, but there's no hosted version.

### 5. The feedback loop is slow

When I say "more geopolitics", the system adjusts my topic weight by +0.2. But I don't see the effect until my *next* briefing. There's no immediate feedback like "Got it — geopolitics weight is now 0.9. Your next briefing will prioritize geopolitical stories."

Actually, looking at the code, the engine does return a results dict (`{"topic:geopolitics": "0.9"}`), and the communication agent does send a confirmation. But the confirmation is generic — it doesn't show the new weight relative to other weights, or what the practical effect will be.

**What I'd want:** After any preference change, show a mini-preview: "Your new topic balance: Geopolitics 90% | AI Policy 70% | Markets 50%. Approximate effect: +2 geopolitics stories, -1 markets story per briefing."

### 6. No easy way to understand what the system is doing

The pipeline runs 8 stages with dozens of sub-operations: research fan-out, credibility scoring, corroboration detection, urgency analysis, expert voting, clustering, geo-risk assessment, trend analysis, article enrichment, editorial review. The user sees none of this. They get a briefing and have to trust it.

The `/status` command shows agent count, expert count, and stage count, but not *what happened* in the last briefing. How many candidates were considered? How many were filtered out and why? Which experts disagreed?

**What I'd want:** A `/debug` or `/transparency` command that shows the pipeline trace for the last briefing:
- "Researched 110 candidates from 18 sources in 1.2s"
- "Credibility filtered 12 low-reliability stories"
- "Expert council: 5 votes per candidate, 3 experts agreed on top pick, 2 dissented"
- "Geo-risk flagged Middle East at 72% (up 8%)"

The audit trail already tracks all of this. It just needs a user-facing view.

---

## What I'd Change If I Were Building This

### Architecture-Level

1. **Real article enrichment is the unlock.** Right now, the system is RSS-summary-deep. The `ArticleEnricher` exists and can fetch/summarize full articles via LLM, but it's the last stage and optional. Moving from "here's the RSS headline and first paragraph" to "here's what this 3,000-word FT analysis actually says" would be transformative. This is the #1 thing that separates a toy from a tool.

2. **The state model needs a database.** JSON files in `state/` work for single-user development, but they won't survive concurrent access, crash recovery, or multi-instance deployment. The SQLite analytics DB exists but isn't used for core state. PostgreSQL for user state and briefing history would enable `/recall`, `/timeline`, `/weekly`, and `/diff` to work with real historical data instead of in-memory approximations.

3. **Async all the way down.** The `_run_sync` helper that detects running event loops and spawns thread pool executors is a workaround. The research agents, article enrichment, and LLM calls all want to be async. The Telegram polling loop is synchronous. Making the engine fully async (and using `asyncio.run` only at the entry point) would simplify the concurrency model and enable true parallel enrichment.

### Product-Level

4. **Topic discovery, not just topic weighting.** The system lets users boost/demote known topics, but it doesn't help users discover topics they didn't know they cared about. The trend detector identifies emerging topics — surfacing these as "You haven't asked about quantum computing, but it spiked 3.2x this week — interested?" would make the system feel alive.

5. **Story continuity across briefings.** The `/tracked` feature is a great start, but it's keyword-matching against future briefings. What users really want is narrative continuity — "Last Tuesday you read about the EU AI Act vote. Here's what happened since: the Parliament committee amended Article 5, three member states objected, and the vote has been delayed to March."  This requires entity-level story tracking, not just keyword matching.

6. **Collaborative intelligence.** The system is single-user. But in a team context (a newsroom, a trading desk, a policy shop), the interesting question is: "What are my colleagues paying attention to that I'm not?" A shared instance with per-user profiles but team-level topic trends would be powerful.

### Code-Level

7. **The communication agent is doing too much.** `communication.py` handles 36 commands with individual handler methods, plus feedback routing, rate limiting, preference updates, analytics recording, and scheduled briefings. It's the largest source file and the one most likely to accumulate bugs. Splitting into `command_handlers.py` (one function per command group) and keeping `communication.py` as the router would help.

8. **The formatter is 1500 lines of string concatenation.** `telegram.py` has 20+ format methods, each building HTML strings line by line. This works, but it's hard to maintain and easy to break HTML nesting. A lightweight template approach (even just multi-line f-strings with helper functions for common patterns) would make the output more predictable.

9. **Tests cover mechanics but not behavior.** The 300+ tests verify that functions return expected types, that configs load, that preferences persist. But there are few tests for the *intelligence quality* — does the expert council actually produce better selections than random? Does corroboration detection work across different headline phrasings? Does the trend detector fire on genuine spikes and not noise? These are harder to test but more important.

---

## The Bottom Line

This is a serious, well-architected system built by someone who thinks carefully about both the intelligence domain and software engineering. The config-driven approach, the multi-expert voting model, the editorial persona stack, and the layered security posture are all evidence of thoughtful design.

The gap between "impressive prototype" and "daily-use tool" is mostly about content quality: replacing template text with real analysis, making adjacent reads meaningful, and getting article enrichment working reliably. The infrastructure is ready — the engine tracks everything, the audit trail captures every decision, the analytics DB records every interaction. The system just needs its intelligence outputs to match the sophistication of its intelligence *infrastructure*.

If I were a human user, I'd be impressed on first use, slightly disappointed by the second briefing (when I notice the repetitive framing), and either drop off or become a power user depending on whether the LLM-backed editorial review is connected. With LLM enrichment on, this could replace my morning news routine. Without it, it's a well-organized RSS reader with great metadata.

The single most impactful thing to build next: make the "why it matters" and "what changed" fields genuinely specific to each story, even without an LLM, using the structured data the pipeline already produces.
