"""Turn a raw incident feed into the facts an SLA credit memo has to state.

The feed is what an on-call rota actually produces: severity written six
different ways, a ticket reopened so the same outage appears twice, an incident
nobody ever closed, one that started last month, and a service that was never
under SLA to begin with. Downtime cannot be summed until that is resolved,
because two tickets covering one outage would bill the customer twice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

SEVERITY_PATTERNS = (
    (re.compile(r"^\s*(sev|p)\s*[-_ ]?\s*1\s*$", re.I), "SEV1"),
    (re.compile(r"^\s*critical\s*$", re.I), "SEV1"),
    (re.compile(r"^\s*(sev|p)\s*[-_ ]?\s*2\s*$", re.I), "SEV2"),
    (re.compile(r"^\s*(major|high)\s*$", re.I), "SEV2"),
    (re.compile(r"^\s*(sev|p)\s*[-_ ]?\s*3\s*$", re.I), "SEV3"),
    (re.compile(r"^\s*(sev|p)\s*[-_ ]?\s*4\s*$", re.I), "SEV4"),
)
UNCLASSIFIED = "UNCLASSIFIED"

# Credit ladder for the enterprise contract. Each entry is the availability
# floor the customer must fall below before that credit rate applies.
CREDIT_LADDER = (
    (95.0, 50),
    (99.0, 25),
    (99.9, 10),
)
REGULATORY_NOTICE_MINUTES = 240


def normalise_severity(raw: object) -> str:
    """Collapse the many spellings of a severity into one vocabulary."""
    text = str(raw or "").strip()
    if not text:
        return UNCLASSIFIED
    for pattern, canonical in SEVERITY_PATTERNS:
        if pattern.match(text):
            return canonical
    return UNCLASSIFIED


def parse_instant(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class Window:
    start: datetime
    end: datetime
    incident_ids: list[str] = field(default_factory=list)
    severity: str = UNCLASSIFIED
    summary: str = ""

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


def _severity_rank(severity: str) -> int:
    order = {"SEV1": 0, "SEV2": 1, "SEV3": 2, "SEV4": 3, UNCLASSIFIED: 4}
    return order.get(severity, 4)


def merge_windows(windows: list[Window]) -> list[Window]:
    """Collapse overlapping and touching windows into single outages.

    Two tickets opened for one outage must count once. The merged window keeps
    the worst severity involved and every contributing incident id, so the memo
    can show which tickets were folded together.
    """
    if not windows:
        return []
    ordered = sorted(windows, key=lambda w: (w.start, w.end))
    merged = [
        Window(ordered[0].start, ordered[0].end, list(ordered[0].incident_ids),
               ordered[0].severity, ordered[0].summary)
    ]
    for window in ordered[1:]:
        current = merged[-1]
        if window.start <= current.end:
            current.end = max(current.end, window.end)
            for incident_id in window.incident_ids:
                if incident_id not in current.incident_ids:
                    current.incident_ids.append(incident_id)
            if _severity_rank(window.severity) < _severity_rank(current.severity):
                current.severity = window.severity
                current.summary = window.summary
        else:
            merged.append(
                Window(window.start, window.end, list(window.incident_ids),
                       window.severity, window.summary)
            )
    return merged


def credit_rate(availability_pct: float, contract_pct: float) -> int:
    """Percentage of the monthly fee owed at this availability."""
    if availability_pct >= contract_pct:
        return 0
    for floor, rate in CREDIT_LADDER:
        if availability_pct < floor:
            return rate
    return 0


def reconcile(feed: dict) -> dict:
    """Produce the per-service facts the template renders."""
    account = feed["account"]
    period = account["billing_period"]
    period_start = parse_instant(period["start"])
    period_end = parse_instant(period["end"])
    period_minutes = int((period_end - period_start).total_seconds() // 60)
    under_sla = set(feed.get("services_under_sla") or [])

    seen_ids: set[str] = set()
    by_service: dict[str, list[Window]] = {}
    excluded: list[dict] = []

    for incident in feed.get("incidents") or []:
        incident_id = str(incident.get("id") or "").strip()
        service = str(incident.get("service") or "").strip()
        severity = normalise_severity(incident.get("severity"))

        if incident_id and incident_id in seen_ids:
            excluded.append({"id": incident_id, "service": service,
                             "reason": "duplicate ticket row"})
            continue
        if incident_id:
            seen_ids.add(incident_id)

        if service not in under_sla:
            excluded.append({"id": incident_id, "service": service,
                             "reason": "service is not under SLA"})
            continue

        start = parse_instant(incident.get("start"))
        if start is None:
            excluded.append({"id": incident_id, "service": service,
                             "reason": "unparseable start time"})
            continue
        # An incident nobody closed is still burning at the period boundary.
        end = parse_instant(incident.get("end")) or period_end
        # Clamp to the billing period so an outage that straddles the boundary
        # is billed to the month that actually carried it.
        start = max(start, period_start)
        end = min(end, period_end)
        if end <= start:
            excluded.append({"id": incident_id, "service": service,
                             "reason": "falls outside the billing period"})
            continue

        by_service.setdefault(service, []).append(
            Window(start, end, [incident_id] if incident_id else [],
                   severity, str(incident.get("summary") or ""))
        )

    contract_pct = float(account["contract_uptime_pct"])
    monthly_fee = float(account["monthly_fee_usd"])
    services = []
    for service in sorted(under_sla):
        merged = merge_windows(by_service.get(service, []))
        down_minutes = sum(window.minutes for window in merged)
        availability = round(
            (period_minutes - down_minutes) / period_minutes * 100, 4
        )
        rate = credit_rate(availability, contract_pct)
        services.append({
            "name": service,
            "outage_count": len(merged),
            "ticket_count": sum(len(w.incident_ids) for w in merged),
            "down_minutes": down_minutes,
            "availability_pct": f"{availability:.4f}",
            "breached": availability < contract_pct,
            "credit_rate_pct": rate,
            "credit_usd": f"{monthly_fee * rate / 100:.2f}",
            "worst_severity": min(
                (w.severity for w in merged), key=_severity_rank, default=UNCLASSIFIED
            ),
            "longest_outage_minutes": max((w.minutes for w in merged), default=0),
            "outages": [{
                "incident_ids": ", ".join(w.incident_ids),
                "merged_from": len(w.incident_ids),
                "severity": w.severity,
                "start": w.start.strftime("%Y-%m-%d %H:%M"),
                "end": w.end.strftime("%Y-%m-%d %H:%M"),
                "minutes": w.minutes,
                "summary": w.summary,
            } for w in merged],
        })

    breached = [s for s in services if s["breached"]]
    total_credit = sum(float(s["credit_usd"]) for s in services)
    regulatory = [
        s for s in services
        if s["worst_severity"] == "SEV1"
        and s["longest_outage_minutes"] > REGULATORY_NOTICE_MINUTES
    ]

    return {
        "Account": [{
            "Name": account["name"],
            "Tier": account["tier"],
            "ContractUptimePct": f"{contract_pct:.2f}",
            "MonthlyFeeUsd": f"{monthly_fee:.2f}",
            "PeriodStart": period_start.strftime("%Y-%m-%d"),
            "PeriodEnd": period_end.strftime("%Y-%m-%d"),
            "PeriodMinutes": period_minutes,
            "BreachCount": len(breached),
            "TotalCreditUsd": f"{total_credit:.2f}",
            "RegulatoryNotice": len(regulatory) > 0,
            "ExcludedCount": len(excluded),
            "Services": services,
            "Excluded": excluded,
        }]
    }
