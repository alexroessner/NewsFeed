from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from newsfeed.models.domain import DeliveryPayload


class JsonFormatter:
    def format(self, payload: DeliveryPayload) -> str:
        data: dict[str, Any] = {
            "user_id": payload.user_id,
            "generated_at": payload.generated_at.isoformat(),
            "briefing_type": payload.briefing_type.value,
            "metadata": payload.metadata,
        }

        data["items"] = []
        for item in payload.items:
            data["items"].append({
                "title": item.candidate.title,
                "source": item.candidate.source,
                "topic": item.candidate.topic,
                "url": item.candidate.url,
                "urgency": item.candidate.urgency.value,
                "lifecycle": item.candidate.lifecycle.value,
                "composite_score": round(item.candidate.composite_score(), 3),
                "why_it_matters": item.why_it_matters,
                "what_changed": item.what_changed,
                "predictive_outlook": item.predictive_outlook,
                "corroborated_by": item.candidate.corroborated_by,
                "regions": item.candidate.regions,
                "confidence": {
                    "low": item.confidence.low,
                    "mid": item.confidence.mid,
                    "high": item.confidence.high,
                    "label": item.confidence.label(),
                    "assumptions": item.confidence.key_assumptions,
                } if item.confidence else None,
                "contrarian_note": item.contrarian_note or None,
                "thread_id": item.thread_id,
            })

        data["threads"] = [
            {
                "thread_id": t.thread_id,
                "headline": t.headline,
                "lifecycle": t.lifecycle.value,
                "urgency": t.urgency.value,
                "source_count": t.source_count,
                "score": round(t.thread_score(), 3),
                "story_count": len(t.candidates),
                "confidence": {
                    "low": t.confidence.low, "mid": t.confidence.mid,
                    "high": t.confidence.high, "label": t.confidence.label(),
                } if t.confidence else None,
            }
            for t in payload.threads
        ]

        data["geo_risks"] = [
            {
                "region": r.region,
                "risk_level": r.risk_level,
                "previous_level": r.previous_level,
                "escalation_delta": r.escalation_delta,
                "is_escalating": r.is_escalating(),
                "drivers": r.drivers,
            }
            for r in payload.geo_risks
        ]

        data["trends"] = [
            {
                "topic": t.topic,
                "velocity": t.velocity,
                "baseline": t.baseline_velocity,
                "anomaly_score": t.anomaly_score,
                "is_emerging": t.is_emerging,
            }
            for t in payload.trends
        ]

        return json.dumps(data, indent=2)
