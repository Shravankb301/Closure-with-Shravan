from __future__ import annotations

from collections import defaultdict

from evidence_delta.analysis import ActiveEvent, source_tier
from evidence_delta.domain import sha256_json

GRAPH_VERSION = "evidence-graph-v1"

FINDING_RELATIONSHIPS = {
    "contradictions": ("conflict", "CONFLICTS_WITHIN", "Conflict"),
    "corroborations": ("corroboration", "SUPPORTS", "Corroborated"),
    "single_source": ("gap", "REVEALS_GAP", "Missing support"),
}


def _humanize(value: str) -> str:
    return value.replace("-", " ").replace("_", " ").title()


def _edge(
    source: str,
    target: str,
    relationship: str,
    *,
    scope: str,
    label: str,
    assertion_count: int | None = None,
) -> dict:
    identity = {
        "source": source,
        "target": target,
        "relationship": relationship,
        "scope": scope,
    }
    result = {
        "id": f"edge:{sha256_json(identity)[:16]}",
        **identity,
        "label": label,
    }
    if assertion_count is not None:
        result["assertion_count"] = assertion_count
    return result


def build_evidence_graph(
    case_id: str,
    case_revision: int,
    events: list[ActiveEvent],
    findings: dict,
) -> dict:
    """Map active ledger rows into a deterministic evidence graph contract."""

    ordered_events = sorted(
        events,
        key=lambda item: (item.occurred_at, item.entity_id, item.assertion_id),
    )
    by_document: dict[str, list[ActiveEvent]] = defaultdict(list)
    by_entity: dict[str, list[ActiveEvent]] = defaultdict(list)
    by_document_entity: dict[tuple[str, str], list[ActiveEvent]] = defaultdict(list)
    for event in ordered_events:
        by_document[event.document_id].append(event)
        by_entity[event.entity_id].append(event)
        by_document_entity[(event.document_id, event.entity_id)].append(event)

    nodes: list[dict] = []
    edges: list[dict] = []

    for document_id, document_events in sorted(by_document.items()):
        document = document_events[0]
        nodes.append(
            {
                "id": f"document:{document_id}",
                "type": "document",
                "label": document.document_filename,
                "data": {
                    "document_id": document_id,
                    "filename": document.document_filename,
                    "source_type": document.document_source_type,
                    "source_uri": document.document_source_uri,
                    "active_assertions": len(document_events),
                },
            }
        )

    for entity_id, entity_events in sorted(by_entity.items()):
        nodes.append(
            {
                "id": f"entity:{entity_id}",
                "type": "entity",
                "label": _humanize(entity_id),
                "data": {
                    "entity_id": entity_id,
                    "active_assertions": len(entity_events),
                    "source_records": len({event.document_id for event in entity_events}),
                },
            }
        )

    for event in ordered_events:
        assertion_node = f"assertion:{event.assertion_id}"
        document_node = f"document:{event.document_id}"
        entity_node = f"entity:{event.entity_id}"
        nodes.append(
            {
                "id": assertion_node,
                "type": "assertion",
                "label": _humanize(event.kind),
                "data": {
                    "assertion_id": event.assertion_id,
                    "document_id": event.document_id,
                    "document_filename": event.document_filename,
                    "source_type": event.document_source_type,
                    "source_uri": event.document_source_uri,
                    "entity_id": event.entity_id,
                    "occurred_at": event.occurred_at,
                    "kind": event.kind,
                    "value": event.value,
                    "time_precision": event.time_precision,
                    "source_locator": event.source_locator,
                    "source_text": event.source_text,
                    "tier": source_tier(event.document_source_type, event.kind),
                },
            }
        )
        edges.extend(
            [
                _edge(
                    document_node,
                    assertion_node,
                    "CONTAINS",
                    scope="detail",
                    label="contains",
                ),
                _edge(
                    assertion_node,
                    entity_node,
                    "DESCRIBES",
                    scope="detail",
                    label="describes",
                ),
            ]
        )

    for (document_id, entity_id), mapped_events in sorted(by_document_entity.items()):
        count = len(mapped_events)
        edges.append(
            _edge(
                f"document:{document_id}",
                f"entity:{entity_id}",
                "MAPS_TO",
                scope="summary",
                label=f"{count} assertion{'s' if count != 1 else ''}",
                assertion_count=count,
            )
        )

    finding_count = 0
    for category, (finding_type, relationship, label_prefix) in FINDING_RELATIONSHIPS.items():
        for finding in findings.get(category, []):
            finding_count += 1
            finding_node = f"finding:{finding['finding_id']}"
            nodes.append(
                {
                    "id": finding_node,
                    "type": "finding",
                    "label": f"{label_prefix}: {_humanize(finding['entity_id'])}",
                    "data": {
                        "category": category,
                        "finding_type": finding_type,
                        "finding": finding,
                    },
                }
            )
            for event in finding["events"]:
                edges.append(
                    _edge(
                        f"assertion:{event['assertion_id']}",
                        finding_node,
                        relationship,
                        scope="detail",
                        label={
                            "CONFLICTS_WITHIN": "conflicts within",
                            "REVEALS_GAP": "reveals gap",
                        }.get(relationship, "supports"),
                    )
                )
            edges.append(
                _edge(
                    f"entity:{finding['entity_id']}",
                    finding_node,
                    "PRODUCES_FINDING",
                    scope="summary",
                    label="produces finding",
                )
            )

    nodes.sort(key=lambda item: (item["type"], item["id"]))
    edges.sort(
        key=lambda item: (
            item["scope"],
            item["source"],
            item["target"],
            item["relationship"],
        )
    )
    return {
        "case_id": case_id,
        "case_revision": case_revision,
        "graph_version": GRAPH_VERSION,
        "mapping": {
            "engine": "deterministic-evidence-mapper-v1",
            "input": "active persistent assertions",
            "active_only": True,
            "relationship_types": sorted({edge["relationship"] for edge in edges}),
        },
        "generated_from": {
            "documents": len(by_document),
            "assertions": len(ordered_events),
            "entities": len(by_entity),
            "findings": finding_count,
        },
        "nodes": nodes,
        "edges": edges,
    }
