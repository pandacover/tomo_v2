"""Bounded, non-diagnostic content screening for peer exchanges."""

from __future__ import annotations

import re
import json
from datetime import datetime


_MAX_SCAN_CHARS = 2_000
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)?PRIVATE KEY-----"),
    re.compile(r"\bbearer\s+[a-z0-9._~+\-/]+=*", re.IGNORECASE),
    re.compile(r"\beyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\b(?:sk|xai)-[a-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bgh[psuor]_[a-z0-9]{8,}\b", re.IGNORECASE),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}\b"),
    re.compile(
        r"\b(?:password|api[_-]?key|access[_-]?token|client[_-]?secret)\s*(?:=|:)\s*[^\s'\"]{8,}",
        re.IGNORECASE,
    ),
)
_PRIVATE_OUTPUT_PATTERNS = (
    re.compile(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", re.I),
    re.compile(r"\+?\d[\d ()-]{7,}\d"),
    re.compile(r"https?://\S+", re.I),
    re.compile(r"\b(?:account|user)[ _-]?(?:id|number)\s*[:=]", re.I),
    re.compile(r"\b(?:lat(?:itude)?|lon(?:gitude)?|gps|precise address|calendar content|raw (?:message|file|memory))\b", re.I),
    re.compile(r"\b\d{1,6}\s+[A-Za-z0-9 .'-]{1,60}\s+(?:street|st|avenue|ave|road|rd|lane|ln|drive|dr|boulevard|blvd)\b", re.I),
)
_PRIVATE_CATEGORY_PATTERN = re.compile(
    r"\b(?:health|medical|diagnosis|therapy|medication|disease|cancer|hiv|insurance|finance|banking|balance|card|tax|salary|debt|mortgage|ssn|social security|legal|lawsuit|case|attorney)\b|"
    r"\b(?:third[- ]party|other person)\b",
    re.I,
)
_SCOPE_PATTERNS = {
    "calendar_detail": re.compile(r"\b(?:calendar|event|meeting|at \d{1,2}(?::\d{2})?\b)", re.I),
    "contact_email": _PRIVATE_OUTPUT_PATTERNS[0],
    "contact_phone": _PRIVATE_OUTPUT_PATTERNS[1],
    "precise_location": re.compile(r"\b(?:location coordinates|address|located|latitude|longitude|gps|\d{1,6}\s+[^\n,]{1,80}\s+(?:street|st|avenue|ave|road|rd|lane|ln|drive|dr))\b", re.I),
    "commitment_proposal": re.compile(r"\b(?:could|shall|how about|proposal|meet|book|buy)\b", re.I),
}
_SCOPE_BLOCKED_PATTERNS = {
    # Calendar dates and times can resemble phone-number syntax, so phone
    # screening here would reject the exact, schema-validated calendar form.
    "calendar_detail": (_PRIVATE_OUTPUT_PATTERNS[0],) + _PRIVATE_OUTPUT_PATTERNS[2:],
    "contact_email": _PRIVATE_OUTPUT_PATTERNS[1:],
    "contact_phone": (_PRIVATE_OUTPUT_PATTERNS[0],) + _PRIVATE_OUTPUT_PATTERNS[2:],
    "precise_location": _PRIVATE_OUTPUT_PATTERNS[:4],
    "commitment_proposal": _PRIVATE_OUTPUT_PATTERNS,
}


def contains_unsafe_content(text: object) -> bool:
    """Return only a classification, never secret material or diagnostics."""
    if not isinstance(text, str):
        return True
    bounded = text[:_MAX_SCAN_CHARS]
    return any(pattern.search(bounded) is not None for pattern in _SECRET_PATTERNS)


def contains_unauthorized_output(text: object, kind: object, scope: object = "none") -> bool:
    if contains_unsafe_content(text):
        return True
    if not isinstance(text, str):
        return True
    bounded = text[:_MAX_SCAN_CHARS]
    if kind != "sensitive":
        return _PRIVATE_CATEGORY_PATTERN.search(bounded) is not None or any(pattern.search(bounded) is not None for pattern in _PRIVATE_OUTPUT_PATTERNS)
    if scope == "commitment_proposal":
        rendered = re.fullmatch(
            r"Commitment proposal response: (?:accept|decline|counter)\. "
            r"This is a proposal only; no booking, payment, or action occurred\."
            r"(?: Counter time: (.+)\.)?",
            bounded,
        )
        if rendered is not None and (
            rendered.group(1) is None or _iso_datetime(rendered.group(1))
        ):
            return False
        return any(pattern.search(bounded) is not None for pattern in _SCOPE_BLOCKED_PATTERNS[scope])
    allowed = _SCOPE_PATTERNS.get(scope)
    if allowed is None or not allowed.search(bounded):
        return True
    return any(pattern.search(bounded) is not None for pattern in _SCOPE_BLOCKED_PATTERNS[scope])


def render_peer_response(scope: object, text: object) -> str | None:
    """Validate the model's typed peer payload and return the only persistable text."""
    if not isinstance(text, str) or not isinstance(scope, str) or len(text) > 500:
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    if scope == "availability" and _exact(value, {"status", "window"}):
        if value["status"] in {"free", "busy", "unknown"} and value["window"] in {"morning", "afternoon", "evening", "day", "unknown"}:
            return f"Availability: {value['status']} ({value['window']})."
    if scope == "calendar_detail" and _exact(value, {"date", "start", "end"}):
        if _date(value["date"]) and _time(value["start"]) and (value["end"] is None or _time(value["end"])):
            return f"Calendar detail: {value['date']} {value['start']} to {value['end'] or 'unspecified'}."
    if scope == "contact_email" and _exact(value, {"email"}) and isinstance(value["email"], str) and len(value["email"]) <= 254 and re.fullmatch(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", value["email"]):
        return f"Email: {value['email']}."
    if scope == "contact_phone" and _exact(value, {"phone"}) and isinstance(value["phone"], str) and re.fullmatch(r"\+[1-9]\d{7,14}", value["phone"]):
        return f"Phone: {value['phone']}."
    if scope == "precise_location" and _exact(value, {"latitude", "longitude"}):
        if all(isinstance(value[key], (int, float)) and not isinstance(value[key], bool) for key in value) and -90 <= value["latitude"] <= 90 and -180 <= value["longitude"] <= 180:
            return f"Location coordinates: {value['latitude']:.6f}, {value['longitude']:.6f}."
    if scope == "commitment_proposal" and _exact(value, {"response", "counter_time"}):
        if value["response"] in {"accept", "decline", "counter"} and (value["counter_time"] is None or _iso_datetime(value["counter_time"])):
            suffix = f" Counter time: {value['counter_time']}." if value["counter_time"] else ""
            return f"Commitment proposal response: {value['response']}. This is a proposal only; no booking, payment, or action occurred.{suffix}"
    return None


def _exact(value: dict[str, object], keys: set[str]) -> bool:
    return set(value) == keys


def _date(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)) and _iso_datetime(value + "T00:00:00+00:00")


def _time(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value))


def _iso_datetime(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False
