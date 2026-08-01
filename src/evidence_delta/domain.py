from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

TIMELINE_PREFIX = "timeline"


@dataclass(frozen=True)
class AssertionView:
    assertion_id: str
    document_id: str
    entity_id: str
    occurred_at: datetime
    kind: str
    value: str
    source_locator: str
    source_text: str


def timeline_key(entity_id: str, occurred_at: datetime) -> str:
    return f"{TIMELINE_PREFIX}:{entity_id}:{occurred_at.date().isoformat()}"


def parse_timeline_key(key: str) -> tuple[str, str]:
    prefix, remainder = key.split(":", maxsplit=1)
    if prefix != TIMELINE_PREFIX:
        raise ValueError(f"Unsupported artifact key: {key}")
    entity_id, day = remainder.rsplit(":", maxsplit=1)
    return entity_id, day


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def build_timeline(
    artifact_key: str, assertions: Iterable[AssertionView]
) -> tuple[dict, list[dict], str]:
    """Pure deterministic artifact derivation.

    The same ordered assertion set always produces the same payload, lineage,
    and input fingerprint. No model call is allowed in this path.
    """

    entity_id, day = parse_timeline_key(artifact_key)
    ordered = sorted(assertions, key=lambda item: (item.occurred_at, item.assertion_id))

    events = [
        {
            "assertion_id": item.assertion_id,
            "occurred_at": iso_utc(item.occurred_at),
            "kind": item.kind,
            "value": item.value,
        }
        for item in ordered
    ]
    lineage = [
        {
            "assertion_id": item.assertion_id,
            "document_id": item.document_id,
            "source_locator": item.source_locator,
            "source_text": item.source_text,
        }
        for item in ordered
    ]
    payload = {
        "artifact_type": "ENTITY_DAY_TIMELINE",
        "entity_id": entity_id,
        "date": day,
        "events": events,
    }
    fingerprint = sha256_json(
        {
            "artifact_key": artifact_key,
            "payload": payload,
            "lineage": lineage,
            "deriver_version": "timeline-v1",
        }
    )
    return payload, lineage, fingerprint
