from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from evidence_delta.domain import sha256_json

ANALYSIS_VERSION = "findings-v1"

# Structured event classes are derived from the assertion kind alone. The
# mapping is an explicit allowlist so a new kind never silently joins a
# conflict rule.
EVENT_CLASS_SUFFIXES = {
    "DISPOSAL": "disposal",
    "CONCEALMENT": "concealment",
    "RECOVERY": "recovery",
    "TRANSFER": "transfer",
    "TRANSFERRED": "transfer",
    "AGREEMENT": "agreement",
}

# Two classes conflict only when the same entity cannot satisfy both accounts
# on the same day. The table is deliberately minimal: a flagged pair is a
# review prompt for the assigned officer, never an automatic falsehood.
CONFLICTING_CLASS_PAIRS = frozenset({frozenset({"disposal", "concealment"})})


@dataclass(frozen=True)
class ActiveEvent:
    """One active (non-retracted) assertion joined with its source document."""

    assertion_id: str
    document_id: str
    document_filename: str
    document_source_type: str
    document_source_uri: str | None
    entity_id: str
    occurred_at: str
    kind: str
    value: str
    time_precision: str
    source_locator: str
    source_text: str

    @property
    def day(self) -> str:
        return self.occurred_at[:10]


def event_class(kind: str) -> str | None:
    for suffix, name in EVENT_CLASS_SUFFIXES.items():
        if kind == suffix or kind.endswith(f"_{suffix}"):
            return name
    return None


def source_tier(source_type: str, kind: str) -> str:
    """Mirror of the interface's legal-status tiers, kept structural."""

    if source_type == "court_outcome_record" and kind.startswith("COURT_"):
        return "court"
    if source_type in {"criminal_complaint", "indictment_announcement"}:
        return "allegation"
    if source_type == "agency_case_history":
        return "agency"
    if source_type.startswith(("officer_", "user_")):
        return "officer"
    if kind.startswith("COURT_"):
        return "court"
    if kind.startswith(("COMPLAINT_", "INDICTMENT_")):
        return "allegation"
    return "agency"


def _event_payload(event: ActiveEvent) -> dict:
    return {
        "assertion_id": event.assertion_id,
        "document_id": event.document_id,
        "document_filename": event.document_filename,
        "source_type": event.document_source_type,
        "source_uri": event.document_source_uri,
        "tier": source_tier(event.document_source_type, event.kind),
        "occurred_at": event.occurred_at,
        "kind": event.kind,
        "event_class": event_class(event.kind),
        "value": event.value,
        "time_precision": event.time_precision,
        "source_locator": event.source_locator,
        "source_text": event.source_text,
    }


def _finding_id(finding_type: str, entity_id: str, day: str, assertion_ids: list[str]) -> str:
    return sha256_json(
        {
            "type": finding_type,
            "entity_id": entity_id,
            "day": day,
            "assertion_ids": sorted(assertion_ids),
            "analysis_version": ANALYSIS_VERSION,
        }
    )[:12]


def _document_ids(events: list[ActiveEvent]) -> list[str]:
    return sorted({item.document_id for item in events})


def _events_by_class(events: list[ActiveEvent]) -> dict[str, list[ActiveEvent]]:
    grouped: dict[str, list[ActiveEvent]] = defaultdict(list)
    for item in events:
        cls = event_class(item.kind)
        if cls is not None:
            grouped[cls].append(item)
    return grouped


def _contradiction_finding(
    entity_id: str,
    day: str,
    left: str,
    right: str,
    events: list[ActiveEvent],
) -> dict:
    documents = _document_ids(events)
    classes = sorted([left, right])
    return {
        "finding_id": _finding_id(
            "CONTRADICTION_CANDIDATE",
            entity_id,
            day,
            [item.assertion_id for item in events],
        ),
        "type": "CONTRADICTION_CANDIDATE",
        "entity_id": entity_id,
        "date": day,
        "classes": classes,
        "summary": (
            f"Active sources assert both {left} and {right} of {entity_id} on {day}. "
            "The accounts cannot both hold; review each cited locator before relying on either."
        ),
        "documents": documents,
        "events": [_event_payload(item) for item in events],
        "support": {
            "level": "CONFLICTED",
            "probability": None,
            "basis": (
                "Active sources support mutually exclusive event classes. "
                "A reviewer must adjudicate the cited accounts."
            ),
        },
        "reasoning": {
            "method": "deterministic_rule",
            "rule_id": "conflicting-event-classes-v1",
            "test": (
                "Flag when active assertions concern the same entity and day "
                "and their event classes are an allowlisted conflicting pair."
            ),
            "premises": [
                {"label": "Entity", "value": entity_id},
                {"label": "Day", "value": day},
                {"label": "Conflicting classes", "value": " vs ".join(classes)},
                {"label": "Distinct sources", "value": str(len(documents))},
            ],
        },
    }


def _contradictions(
    entity_id: str,
    day: str,
    events_by_class: dict[str, list[ActiveEvent]],
) -> list[dict]:
    findings = []
    present = sorted(events_by_class)
    for index, left in enumerate(present):
        for right in present[index + 1 :]:
            if frozenset({left, right}) not in CONFLICTING_CLASS_PAIRS:
                continue
            findings.append(
                _contradiction_finding(
                    entity_id,
                    day,
                    left,
                    right,
                    events_by_class[left] + events_by_class[right],
                )
            )
    return findings


def _corroboration_finding(
    entity_id: str,
    day: str,
    cls: str,
    events: list[ActiveEvent],
) -> dict | None:
    documents = _document_ids(events)
    if len(documents) < 2:
        return None
    tiers = sorted({source_tier(item.document_source_type, item.kind) for item in events})
    cross_tier = len(tiers) > 1
    return {
        "finding_id": _finding_id(
            "CORROBORATION",
            entity_id,
            day,
            [item.assertion_id for item in events],
        ),
        "type": "CORROBORATION",
        "entity_id": entity_id,
        "date": day,
        "event_class": cls,
        "summary": (
            f"{len(documents)} distinct source records support the {cls} of {entity_id} on {day}"
            + (f" across independent tiers ({', '.join(tiers)})." if cross_tier else ".")
        ),
        "documents": documents,
        "tiers": tiers,
        "cross_tier": cross_tier,
        "events": [_event_payload(item) for item in events],
        "support": {
            "level": "STRONG" if cross_tier else "MODERATE",
            "probability": None,
            "basis": (
                "Distinct records across multiple legal-status tiers support "
                "the same structured event."
                if cross_tier
                else "Multiple distinct records in one legal-status tier support "
                "the same structured event."
            ),
        },
        "reasoning": {
            "method": "deterministic_rule",
            "rule_id": "distinct-source-corroboration-v1",
            "test": (
                "Surface when two or more active source records support the same "
                "entity, day, and event class."
            ),
            "premises": [
                {"label": "Entity", "value": entity_id},
                {"label": "Day", "value": day},
                {"label": "Event class", "value": cls},
                {"label": "Distinct sources", "value": str(len(documents))},
                {"label": "Legal-status tiers", "value": ", ".join(tiers)},
            ],
        },
    }


def _corroborations(
    entity_id: str,
    day: str,
    events_by_class: dict[str, list[ActiveEvent]],
) -> list[dict]:
    findings = [
        _corroboration_finding(entity_id, day, cls, events_by_class[cls])
        for cls in sorted(events_by_class)
    ]
    return [finding for finding in findings if finding is not None]


def _single_source_finding(
    entity_id: str,
    day: str,
    events: list[ActiveEvent],
) -> dict | None:
    documents = _document_ids(events)
    if len(documents) != 1:
        return None
    return {
        "finding_id": _finding_id(
            "SINGLE_SOURCE",
            entity_id,
            day,
            [item.assertion_id for item in events],
        ),
        "type": "SINGLE_SOURCE",
        "entity_id": entity_id,
        "date": day,
        "summary": (
            f"Every active event for {entity_id} on {day} rests on one source record. "
            "Corroborate before treating it as load-bearing."
        ),
        "documents": documents,
        "events": [_event_payload(item) for item in events],
        "support": {
            "level": "LIMITED",
            "probability": None,
            "basis": ("Every active event for this entity and day depends on one source record."),
        },
        "reasoning": {
            "method": "deterministic_rule",
            "rule_id": "single-source-exposure-v1",
            "test": (
                "Surface when all active assertions for an entity and day come "
                "from exactly one source record."
            ),
            "premises": [
                {"label": "Entity", "value": entity_id},
                {"label": "Day", "value": day},
                {"label": "Distinct sources", "value": "1"},
                {"label": "Active assertions", "value": str(len(events))},
            ],
        },
    }


def _group_events(events: list[ActiveEvent]) -> list[tuple[tuple[str, str], list[ActiveEvent]]]:
    grouped: dict[tuple[str, str], list[ActiveEvent]] = defaultdict(list)
    for item in events:
        grouped[(item.entity_id, item.day)].append(item)
    return sorted(grouped.items(), key=lambda pair: (pair[0][1], pair[0][0]))


def derive_findings(events: list[ActiveEvent]) -> dict:
    """Return deterministic review prompts for one active assertion set."""

    ordered = sorted(events, key=lambda item: (item.day, item.entity_id, item.assertion_id))
    contradictions: list[dict] = []
    corroborations: list[dict] = []
    single_source: list[dict] = []

    for (entity_id, day), group in _group_events(ordered):
        events_by_class = _events_by_class(group)
        contradictions.extend(_contradictions(entity_id, day, events_by_class))
        corroborations.extend(_corroborations(entity_id, day, events_by_class))
        finding = _single_source_finding(entity_id, day, group)
        if finding is not None:
            single_source.append(finding)

    return {
        "analysis_version": ANALYSIS_VERSION,
        "generated_from": {
            "active_assertions": len(ordered),
            "documents": len({item.document_id for item in ordered}),
        },
        "contradictions": contradictions,
        "corroborations": corroborations,
        "single_source": single_source,
    }
