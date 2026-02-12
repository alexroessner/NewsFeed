from __future__ import annotations

from newsfeed.models.domain import DeliveryPayload


class TelegramFormatter:
    def format(self, payload: DeliveryPayload) -> str:
        lines = [f"NewsFeed Brief ({payload.generated_at.isoformat()})", ""]
        for idx, item in enumerate(payload.items, start=1):
            lines.append(f"{idx}. {item.candidate.title} [{item.candidate.source}]")
            lines.append(f"   Why it matters: {item.why_it_matters}")
            lines.append(f"   Changed: {item.what_changed}")
            lines.append(f"   Outlook: {item.predictive_outlook}")
            if item.adjacent_reads:
                lines.append("   Adjacent reads:")
                for read in item.adjacent_reads:
                    lines.append(f"   - {read}")
            lines.append("")
        return "\n".join(lines).strip()
