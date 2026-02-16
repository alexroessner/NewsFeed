"""Lightweight named entity extraction for intelligence cross-referencing.

Extracts people, organizations, and countries/regions from story text
using pattern-based recognition (no ML model dependency). Designed for
fast extraction from headlines and summaries, not full NER accuracy.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any


# ── Known entities for high-confidence matching ──

_WORLD_LEADERS = frozenset({
    "Biden", "Trump", "Putin", "Xi Jinping", "Zelensky", "Macron",
    "Scholz", "Sunak", "Starmer", "Modi", "Erdogan", "Netanyahu",
    "Kishida", "Lula", "Milei", "Meloni", "Trudeau", "Albanese",
    "Kim Jong Un", "Khamenei", "MBS", "bin Salman",
})

_MAJOR_ORGS = frozenset({
    "NATO", "EU", "UN", "WHO", "IMF", "ECB", "OPEC", "BRICS",
    "Fed", "Federal Reserve", "SEC", "DOJ", "FBI", "CIA", "NSA",
    "Pentagon", "Kremlin", "White House", "Congress",
    "Apple", "Google", "Microsoft", "Amazon", "Meta", "NVIDIA",
    "Tesla", "OpenAI", "Anthropic", "SpaceX", "Samsung",
    "TSMC", "Intel", "AMD", "Huawei", "ByteDance", "TikTok",
    "Goldman Sachs", "JPMorgan", "BlackRock", "Berkshire Hathaway",
    "Boeing", "Lockheed Martin", "Raytheon",
    "Hamas", "Hezbollah", "Wagner", "ISIS",
})

_COUNTRIES = frozenset({
    "United States", "US", "USA", "China", "Russia", "Ukraine",
    "Taiwan", "Israel", "Iran", "North Korea", "South Korea",
    "Japan", "India", "Pakistan", "Saudi Arabia", "Turkey",
    "Germany", "France", "UK", "Britain", "Italy", "Poland",
    "Brazil", "Mexico", "Argentina", "Australia", "Canada",
    "Syria", "Iraq", "Afghanistan", "Libya", "Yemen", "Sudan",
    "Nigeria", "South Africa", "Egypt", "Ethiopia",
    "Philippines", "Indonesia", "Vietnam", "Thailand", "Myanmar",
    "Palestine", "Gaza", "Lebanon",
})

# Pattern for capitalized multi-word names (potential entities)
_NAME_PATTERN = re.compile(
    r"\b([A-Z][a-z]+(?:\s+(?:(?:al-|bin\s|von\s|de\s|van\s)?[A-Z][a-z]+))+)\b"
)


def extract_entities(text: str) -> dict[str, set[str]]:
    """Extract entities from text, returning {category: {entity_names}}.

    Categories: 'people', 'organizations', 'countries'
    """
    result: dict[str, set[str]] = {
        "people": set(),
        "organizations": set(),
        "countries": set(),
    }

    if not text:
        return result

    # Check known entities first (exact match)
    for leader in _WORLD_LEADERS:
        if leader in text:
            result["people"].add(leader)

    for org in _MAJOR_ORGS:
        if org in text:
            result["organizations"].add(org)

    for country in _COUNTRIES:
        if country in text:
            result["countries"].add(country)

    # Pattern-based: find capitalized multi-word names not already matched
    known_all = result["people"] | result["organizations"] | result["countries"]
    for match in _NAME_PATTERN.finditer(text):
        name = match.group(1).strip()
        if name in known_all:
            continue
        # Skip common non-entity phrases
        if _is_noise(name):
            continue
        # Heuristic: 2-3 word capitalized names are likely people
        words = name.split()
        if 2 <= len(words) <= 3:
            result["people"].add(name)

    return result


def _is_noise(name: str) -> bool:
    """Filter out common false-positive entity matches."""
    noise = {
        "New York", "Los Angeles", "San Francisco", "Hong Kong",
        "Middle East", "South China", "North America", "South America",
        "East Asia", "Central Asia", "West Bank", "Red Sea",
        "Wall Street", "Silicon Valley", "Capitol Hill",
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
        "Saturday", "Sunday", "January", "February", "March",
        "April", "May", "June", "July", "August", "September",
        "October", "November", "December",
        "According To", "In The", "The United",
    }
    return name in noise


def build_entity_map(items: list, start_index: int = 1) -> dict[str, list[int]]:
    """Build entity-to-story-index mapping across briefing items.

    Returns: {"Entity Name": [1, 3, 7]} where numbers are 1-based story indices.
    Entities appearing in only one story are excluded (no cross-reference value).
    """
    entity_stories: dict[str, list[int]] = defaultdict(list)

    for idx, item in enumerate(items, start=start_index):
        c = item.candidate
        text = f"{c.title} {c.summary}"
        entities = extract_entities(text)
        for category in ("people", "organizations", "countries"):
            for entity in entities[category]:
                entity_stories[entity].append(idx)

    # Only keep entities appearing in 2+ stories
    return {
        entity: indices
        for entity, indices in entity_stories.items()
        if len(indices) >= 2
    }


def format_entity_dashboard(items: list) -> dict[str, Any]:
    """Build full entity analysis for the entity dashboard.

    Returns structured data for the formatter:
    {
        "people": {name: [story_indices]},
        "organizations": {name: [story_indices]},
        "countries": {name: [story_indices]},
        "connections": [(entity_a, entity_b, shared_stories)],
        "total_entities": int,
    }
    """
    people: dict[str, list[int]] = defaultdict(list)
    organizations: dict[str, list[int]] = defaultdict(list)
    countries: dict[str, list[int]] = defaultdict(list)

    for idx, item in enumerate(items, start=1):
        c = item.candidate
        text = f"{c.title} {c.summary}"
        entities = extract_entities(text)
        for entity in entities["people"]:
            people[entity].append(idx)
        for entity in entities["organizations"]:
            organizations[entity].append(idx)
        for entity in entities["countries"]:
            countries[entity].append(idx)

    # Find entity connections (entities that co-occur in stories)
    # Cap entities to prevent O(n²) connection building with large inputs.
    # Keep only the most-referenced entities (appear in most stories).
    _MAX_ENTITIES_FOR_CONNECTIONS = 50
    all_entities: dict[str, set[int]] = {}
    for name, indices in {**people, **organizations, **countries}.items():
        all_entities[name] = set(indices)

    # If too many entities, keep only top N by story count
    if len(all_entities) > _MAX_ENTITIES_FOR_CONNECTIONS:
        sorted_ents = sorted(all_entities.items(), key=lambda kv: -len(kv[1]))
        all_entities = dict(sorted_ents[:_MAX_ENTITIES_FOR_CONNECTIONS])

    connections: list[tuple[str, str, int]] = []
    entity_names = list(all_entities.keys())
    for i, name_a in enumerate(entity_names):
        for name_b in entity_names[i + 1:]:
            shared = all_entities[name_a] & all_entities[name_b]
            if len(shared) >= 2:
                connections.append((name_a, name_b, len(shared)))

    connections.sort(key=lambda x: -x[2])

    return {
        "people": {k: v for k, v in people.items() if len(v) >= 2},
        "organizations": {k: v for k, v in organizations.items() if len(v) >= 2},
        "countries": {k: v for k, v in countries.items() if len(v) >= 2},
        "connections": connections[:10],
        "total_entities": len(all_entities),
    }
