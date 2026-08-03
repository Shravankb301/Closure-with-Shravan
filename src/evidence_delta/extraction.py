"""AI-assisted evidence extraction.

Turns an unstructured evidence excerpt (a report paragraph, a jail-call
transcript, a warrant return) into *proposed* structured assertions. Every
proposal quotes the exact supporting span from the source, and nothing here
writes to the immutable ledger: the model only proposes, a human confirms, and
the confirmed rows enter through the normal ``POST /cases/{id}/documents`` path.

If the Anthropic SDK and an API key are present, extraction uses Claude. When
they are not — the default for local demos, tests, and the hosted build — a
deterministic keyword-and-date extractor produces the same proposal shape so
the workflow always works offline.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime

MODEL = "claude-opus-4-8"

PRECISIONS = ("EXACT", "MINUTE", "HOUR", "DAY", "MONTH", "WINDOW", "UNKNOWN")
CONFIDENCE = ("high", "medium", "low")

# Structured event kinds the deterministic findings engine understands. The
# suffixes here line up with analysis.EVENT_CLASS_SUFFIXES so extracted
# assertions can immediately participate in contradiction/corroboration review.
_KEYWORD_KINDS: tuple[tuple[str, str], ...] = (
    (r"dispos|threw away|discard|dump|got rid of|toss", "REPORTED_DISPOSAL"),
    (r"conceal|hid|hidden|stash|cover(ed)? up", "REPORTED_CONCEALMENT"),
    (r"recover|seiz|retriev|found|located", "REPORTED_RECOVERY"),
    (r"transfer|hand(ed)? over|gave|pass(ed)? (it |the )", "REPORTED_TRANSFER"),
    (r"agree|arrang|plann?ed to|conspir", "REPORTED_AGREEMENT"),
    (r"remov|took|carried|brought", "REMOVED_FROM"),
    (r"observ|saw|witness|noticed", "OBSERVED_AT"),
)

# Known Boston-case entities so the offline extractor still proposes the slugs
# the graph and findings engine already recognize.
_KNOWN_ENTITIES: tuple[tuple[str, str], ...] = (
    (r"backpack|fireworks", "backpack"),
    (r"laptop", "laptop-computer"),
    (r"dorm", "dzhokhar-dorm-room"),
    (r"new bedford|apartment", "new-bedford-apartment"),
    (r"kadyrbayev", "dias-kadyrbayev"),
    (r"tazhayakov", "azamat-tazhayakov"),
    (r"tsarnaev", "dzhokhar-tsarnaev"),
)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def model_available() -> bool:
    """Whether a real Claude extraction can run in this environment."""

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def extract_assertions(
    text: str,
    *,
    filename: str | None = None,
    source_hint: str | None = None,
    allow_model: bool = True,
) -> dict:
    """Return proposed assertions for a raw evidence excerpt.

    Never raises on extraction quality: a model failure degrades to the
    deterministic extractor so the intake workflow always returns something.
    """

    excerpt = text.strip()
    if allow_model and model_available():
        try:
            proposals = _extract_with_model(excerpt, source_hint)
            return {
                "mode": "assisted",
                "model": MODEL,
                "reason": "Extracted with Claude; every span is human-confirmable.",
                "filename": filename,
                "proposals": [_finalize(item, excerpt) for item in proposals],
            }
        except Exception as error:  # noqa: BLE001 - degrade, never fail intake
            fallback_reason = (
                f"Claude extraction was unavailable ({type(error).__name__}); "
                "used the deterministic extractor instead."
            )
    else:
        fallback_reason = (
            "Deterministic extractor (set ANTHROPIC_API_KEY and install "
            "'anthropic' to propose with Claude)."
        )

    proposals = _extract_deterministic(excerpt)
    return {
        "mode": "deterministic",
        "model": None,
        "reason": fallback_reason,
        "filename": filename,
        "proposals": [_finalize(item, excerpt) for item in proposals],
    }


def _extract_with_model(excerpt: str, source_hint: str | None) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic()
    context = f"\nSource context: {source_hint}" if source_hint else ""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "assertions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "entity_id": {"type": "string"},
                        "occurred_at": {"type": "string"},
                        "kind": {"type": "string"},
                        "value": {"type": "string"},
                        "time_precision": {"type": "string", "enum": list(PRECISIONS)},
                        "source_text": {"type": "string"},
                        "source_locator": {"type": "string"},
                        "confidence": {"type": "string", "enum": list(CONFIDENCE)},
                        "rationale": {"type": "string"},
                    },
                    "required": [
                        "entity_id", "occurred_at", "kind", "value",
                        "time_precision", "source_text", "source_locator",
                        "confidence", "rationale",
                    ],
                },
            }
        },
        "required": ["assertions"],
    }
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
        system=(
            "You are a forensic evidence analyst preparing structured leads for a "
            "human reviewer in a criminal investigation. Extract discrete, "
            "source-backed assertions from the excerpt. Rules: (1) Every assertion "
            "MUST set source_text to a VERBATIM span copied from the excerpt — never "
            "paraphrase it. (2) Do not invent facts, names, or dates not present in "
            "the text. (3) Prefer a coarse time_precision (DAY/MONTH/WINDOW/UNKNOWN) "
            "when the source is vague rather than inventing an exact timestamp; set "
            "occurred_at to ISO 8601 with a UTC offset, or an empty string if no date "
            "is stated. (4) Use a lowercase-hyphenated entity_id for the person, "
            "object, or location the event is about. (5) These are review prompts for "
            "a human, not conclusions — mark confidence honestly. Return an empty list "
            "if nothing concrete is extractable."
        ),
        messages=[{"role": "user", "content": f"Excerpt:{context}\n\n{excerpt}"}],
    )
    payload = json.loads(next(block.text for block in response.content if block.type == "text"))
    return payload.get("assertions", [])


def _extract_deterministic(excerpt: str) -> list[dict]:
    proposals: list[dict] = []
    for index, span in enumerate(_sentences(excerpt), start=1):
        lowered = span.lower()
        kind = None
        for pattern, label in _KEYWORD_KINDS:
            match = re.search(pattern, lowered)
            if match is not None and not _match_is_negated(lowered, match.start()):
                kind = label
                break
        if kind is None:
            continue
        occurred_at, precision = _parse_date(span)
        entity_id = next(
            (slug for pattern, slug in _KNOWN_ENTITIES if re.search(pattern, lowered)),
            "unspecified-entity",
        )
        proposals.append(
            {
                "entity_id": entity_id,
                "occurred_at": occurred_at,
                "kind": kind,
                "value": span if len(span) <= 300 else span[:297] + "...",
                "time_precision": precision,
                "source_text": span,
                "source_locator": f"excerpt:sentence-{index}",
                "confidence": "low",
                "rationale": (
                    "Keyword and date match; confirm the entity and time "
                    "before relying on it."
                ),
            }
        )
    return proposals


def _match_is_negated(text: str, match_start: int) -> bool:
    """Reject simple negated keyword matches in the offline fallback.

    This extractor is deliberately conservative: skipping a possible proposal
    is safer than suggesting the opposite of what a source sentence says. The
    human-confirmation boundary remains required for every accepted assertion.
    """

    prefix = text[max(0, match_start - 80):match_start]
    return re.search(
        r"\b(?:no|not|never|without)\b(?:\W+\w+){0,6}\W*$",
        prefix,
    ) is not None


def _finalize(item: dict, excerpt: str) -> dict:
    """Coerce a raw proposal into a safe, UI-ready shape."""

    source_text = str(item.get("source_text", "")).strip()
    precision = str(item.get("time_precision", "UNKNOWN")).upper()
    if precision not in PRECISIONS:
        precision = "UNKNOWN"
    confidence = str(item.get("confidence", "low")).lower()
    if confidence not in CONFIDENCE:
        confidence = "low"
    return {
        "entity_id": str(item.get("entity_id", "")).strip(),
        "occurred_at": _normalize_occurred_at(item.get("occurred_at")),
        "kind": str(item.get("kind", "")).strip().upper() or "OTHER",
        "value": str(item.get("value", "")).strip() or source_text,
        "time_precision": precision,
        "source_text": source_text,
        "source_locator": str(item.get("source_locator", "excerpt")).strip() or "excerpt",
        "confidence": confidence,
        "rationale": str(item.get("rationale", "")).strip(),
        # A model must quote the source verbatim; surface any span it invented so
        # the reviewer sees the provenance is unverified before confirming.
        "provenance_verified": bool(source_text) and source_text in excerpt,
    }


def _normalize_occurred_at(value: object) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _sentences(excerpt: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", excerpt)
    return [part.strip() for part in parts if part.strip()]


def _parse_date(span: str) -> tuple[str | None, str]:
    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", span)
    if iso:
        return _iso(int(iso[1]), int(iso[2]), int(iso[3])), "DAY"
    month_day_year = re.search(
        r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})\b", span, re.IGNORECASE
    )
    if month_day_year:
        month = _MONTHS[month_day_year[1].lower()]
        return _iso(int(month_day_year[3]), month, int(month_day_year[2])), "DAY"
    month_year = re.search(
        r"\b(" + "|".join(_MONTHS) + r")\s+(\d{4})\b", span, re.IGNORECASE
    )
    if month_year:
        return _iso(int(month_year[2]), _MONTHS[month_year[1].lower()], 1), "MONTH"
    year = re.search(r"\b(19|20)\d{2}\b", span)
    if year:
        return _iso(int(year[0]), 1, 1), "WINDOW"
    return None, "UNKNOWN"


def _iso(year: int, month: int, day: int) -> str | None:
    try:
        return datetime(year, month, day, tzinfo=UTC).isoformat()
    except ValueError:
        return None
