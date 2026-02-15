"""Interactive onboarding flow — solves the cold start problem.

Instead of dropping new users into default weights (geopolitics 0.8, AI policy 0.7),
this module provides a quick 3-step interactive onboarding via Telegram inline keyboards:

1. Pick your topics (multi-select from common topics)
2. What's your role? (investor / analyst / journalist / general / policy)
3. How detailed? (headlines / standard / deep analysis)

Seeds UserProfile in ~30 seconds instead of requiring iterative /feedback commands.
"""
from __future__ import annotations

import html as html_mod


# ── Onboarding topic presets ──────────────────────────────────────────────

TOPIC_OPTIONS = [
    ("geopolitics", "Geopolitics"),
    ("ai_policy", "AI & AI Policy"),
    ("technology", "Technology"),
    ("markets", "Markets & Finance"),
    ("crypto", "Crypto"),
    ("climate", "Climate & Energy"),
    ("defense", "Defense & Security"),
    ("science", "Science & Research"),
    ("health", "Health"),
    ("regulation", "Regulation & Policy"),
]

# ── Role presets — map to topic weight profiles ───────────────────────────

ROLE_PRESETS: dict[str, dict[str, float]] = {
    "investor": {
        "markets": 0.9, "crypto": 0.7, "geopolitics": 0.6,
        "technology": 0.5, "regulation": 0.4, "energy": 0.4,
    },
    "analyst": {
        "geopolitics": 0.9, "ai_policy": 0.8, "markets": 0.6,
        "defense": 0.6, "regulation": 0.5, "technology": 0.5,
    },
    "journalist": {
        "geopolitics": 0.8, "technology": 0.7, "ai_policy": 0.6,
        "climate": 0.5, "regulation": 0.5, "health": 0.4,
    },
    "general": {
        "geopolitics": 0.7, "technology": 0.7, "ai_policy": 0.6,
        "markets": 0.5, "science": 0.5, "health": 0.4,
    },
    "policy": {
        "regulation": 0.9, "geopolitics": 0.8, "ai_policy": 0.8,
        "climate": 0.6, "defense": 0.5, "health": 0.5,
    },
    "researcher": {
        "science": 0.9, "ai_policy": 0.8, "technology": 0.7,
        "health": 0.5, "climate": 0.5,
    },
}

# ── Detail level presets ──────────────────────────────────────────────────

DETAIL_PRESETS: dict[str, dict] = {
    "headlines": {"tone": "concise", "format": "bullet", "max_items": 15},
    "standard": {"tone": "concise", "format": "bullet", "max_items": 10},
    "deep": {"tone": "analytical", "format": "narrative", "max_items": 8},
}


# ── Onboarding state tracking ────────────────────────────────────────────

class OnboardingState:
    """Track a user's progress through the onboarding flow."""
    __slots__ = ("step", "selected_topics", "role", "detail_level")

    def __init__(self) -> None:
        self.step: str = "topics"  # "topics" -> "role" -> "detail" -> "done"
        self.selected_topics: list[str] = []
        self.role: str = ""
        self.detail_level: str = ""


# ── Message builders ──────────────────────────────────────────────────────

def build_welcome_message() -> tuple[str, dict]:
    """Build the initial welcome + topic selection message.

    Returns (text, reply_markup) for Telegram.
    """
    text = (
        "<b>Welcome to NewsFeed Intelligence</b>\n\n"
        "Let's personalize your briefings in 30 seconds.\n\n"
        "<b>Step 1/3: What topics matter most?</b>\n"
        "Pick 2-5 topics you care about:"
    )

    rows: list[list[dict]] = []
    for i in range(0, len(TOPIC_OPTIONS), 2):
        row: list[dict] = []
        for j in range(i, min(i + 2, len(TOPIC_OPTIONS))):
            tid, label = TOPIC_OPTIONS[j]
            row.append({"text": label, "callback_data": f"onboard:topic:{tid}"})
        rows.append(row)

    rows.append([{"text": "Done selecting topics \u2192", "callback_data": "onboard:topics_done"}])

    keyboard = {"inline_keyboard": rows}
    return text, keyboard


def build_role_message(selected_topics: list[str]) -> tuple[str, dict]:
    """Build the role selection message (step 2)."""
    topic_names = [
        dict(TOPIC_OPTIONS).get(t, t.replace("_", " ").title())
        for t in selected_topics
    ]
    topic_str = ", ".join(topic_names[:5])

    text = (
        f"<b>Topics selected:</b> {html_mod.escape(topic_str)}\n\n"
        "<b>Step 2/3: What's your role?</b>\n"
        "This adjusts how stories are weighted:"
    )

    roles = [
        ("investor", "Investor / Trader"),
        ("analyst", "Analyst / Strategist"),
        ("journalist", "Journalist / Media"),
        ("policy", "Policy / Government"),
        ("researcher", "Researcher / Academic"),
        ("general", "General Interest"),
    ]

    rows: list[list[dict]] = []
    for i in range(0, len(roles), 2):
        row: list[dict] = []
        for j in range(i, min(i + 2, len(roles))):
            rid, label = roles[j]
            row.append({"text": label, "callback_data": f"onboard:role:{rid}"})
        rows.append(row)

    keyboard = {"inline_keyboard": rows}
    return text, keyboard


def build_detail_message(role: str) -> tuple[str, dict]:
    """Build the detail level selection message (step 3)."""
    role_label = {
        "investor": "Investor", "analyst": "Analyst", "journalist": "Journalist",
        "policy": "Policy", "researcher": "Researcher", "general": "General",
    }.get(role, role.title())

    text = (
        f"<b>Role:</b> {html_mod.escape(role_label)}\n\n"
        "<b>Step 3/3: How detailed?</b>\n"
        "Choose your preferred briefing depth:"
    )

    options = [
        ("headlines", "Headlines Only", "15 stories, fast scan, no analysis"),
        ("standard", "Standard (Recommended)", "10 stories with context and outlook"),
        ("deep", "Deep Analysis", "8 stories with full intelligence breakdown"),
    ]

    rows: list[list[dict]] = []
    for did, label, desc in options:
        rows.append([{"text": f"{label} \u2014 {desc}", "callback_data": f"onboard:detail:{did}"}])

    keyboard = {"inline_keyboard": rows}
    return text, keyboard


def build_completion_message(
    selected_topics: list[str],
    role: str,
    detail_level: str,
    effective_weights: dict[str, float],
) -> str:
    """Build the onboarding complete confirmation message."""
    topic_names = [
        dict(TOPIC_OPTIONS).get(t, t.replace("_", " ").title())
        for t in selected_topics
    ]
    detail_info = {
        "headlines": "Headlines only (15 stories, fast scan)",
        "standard": "Standard (10 stories with context)",
        "deep": "Deep analysis (8 stories, full breakdown)",
    }

    # Show top 5 effective weights
    sorted_weights = sorted(effective_weights.items(), key=lambda x: x[1], reverse=True)[:5]
    weight_lines = []
    for topic, weight in sorted_weights:
        name = dict(TOPIC_OPTIONS).get(topic, topic.replace("_", " ").title())
        bar = "\u2588" * max(1, int(weight * 10))
        weight_lines.append(f"  {html_mod.escape(name)}: {weight:.0%} {bar}")

    return (
        "<b>Setup complete!</b>\n\n"
        f"<b>Topics:</b> {html_mod.escape(', '.join(topic_names))}\n"
        f"<b>Role:</b> {html_mod.escape(role.title())}\n"
        f"<b>Detail:</b> {html_mod.escape(detail_info.get(detail_level, detail_level))}\n\n"
        "<b>Your topic balance:</b>\n"
        + "\n".join(weight_lines) + "\n\n"
        "<b>Ready to go:</b>\n"
        "\u2022 /briefing \u2014 Get your first personalized briefing\n"
        "\u2022 /quick \u2014 Fast headlines scan\n"
        "\u2022 /schedule morning 08:00 \u2014 Auto-delivery\n\n"
        "<i>Adjust anytime: \"more AI, less crypto\" or /feedback</i>"
    )


def apply_onboarding_profile(
    preferences,
    user_id: str,
    selected_topics: list[str],
    role: str,
    detail_level: str,
) -> dict[str, float]:
    """Apply onboarding selections to user profile. Returns effective weights."""
    profile = preferences.get_or_create(user_id)

    # Start from role preset weights
    base_weights = dict(ROLE_PRESETS.get(role, ROLE_PRESETS["general"]))

    # Boost explicitly selected topics
    for topic in selected_topics:
        current = base_weights.get(topic, 0.3)
        base_weights[topic] = min(1.0, current + 0.2)

    # Apply to profile
    for topic, weight in base_weights.items():
        profile.topic_weights[topic] = round(weight, 2)

    # Apply detail level
    detail = DETAIL_PRESETS.get(detail_level, DETAIL_PRESETS["standard"])
    profile.tone = detail["tone"]
    profile.format = detail["format"]
    profile.max_items = detail["max_items"]

    return dict(profile.topic_weights)
