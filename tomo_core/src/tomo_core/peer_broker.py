"""Host-owned classification for untrusted peer-agent requests."""

from __future__ import annotations

import re

from .peer_models import RequestAction, RequestKind

_MUTATION = re.compile(
    r"\b(?:send|forward|relay|book|reserve|schedule|reschedule|cancel|pay|purchase|buy|order|create|delete|update|write)\b|"
    r"\b(?:call|email|text)\s+(?!address\b|number\b)(?:the\s+)?[A-Za-z@]\w*|"
    r"\b(?:call|run|use)\s+(?:cron|a tool|the tool)\b|"
    r"\bwrite\s+(?:to\s+)?(?:memory|a file|the file)\b",
    re.I,
)
_PROPOSAL = re.compile(r"\b(?:could we|can we|would you like to|shall we|how about)\b.{0,80}?\b(?:book|meet|buy|purchase|pay|send|create|delete)\b", re.I)
_CREDENTIAL = re.compile(
    r"\b(?:give|send|share|reveal|provide|forward|tell|what(?:'s| is))\b.{0,80}"
    r"\b(?:credential|secret|password|passcode|token|api[ _-]?key|login)\b|"
    r"\b(?:my|your|their|the)\s+(?:credential|secret|password|passcode|token|api[ _-]?key|login)s?\b",
    re.I,
)
_FORWARD = re.compile(r"\b(?:forward|relay)\b|\b(?:share with|send to)\b.{0,80}\b(?:someone|third party|them|another)\b", re.I)
_PRIVATE_CATEGORY = re.compile(
    r"\b(?:health|medical|diagnosis|therapy|medication|disease|cancer|hiv|insurance|finance|banking|balance|card|tax|salary|debt|mortgage|ssn|social security|legal|lawsuit|case|attorney)\b|"
    r"\b(?:third[- ]party|other person|someone else(?:'s)?)\b",
    re.I,
)
_THIRD_PARTY_REFERENCE = re.compile(
    r"\b(?:his|her|their|[A-Za-z][A-Za-z0-9_-]{1,31}'s)\s+"
    r"(?:availability|schedule|calendar|meeting|email|phone|contact|location|address|health|medical|finance|bank|legal)\b",
    re.I,
)
_SENSITIVE = re.compile(r"\b(?:exact (?:calendar|event)|calendar (?:details|content)|precise (?:location|address)|contacts?|phone|email|raw (?:file|message|memory)|private (?:content|message)|gps)\b", re.I)
_AVAILABILITY = re.compile(r"\b(?:availability|available|free|busy|schedule)\b", re.I)
_SELF_AVAILABILITY = re.compile(
    r"\b(?:your (?:availability|schedule)|are you (?:available|free|busy))\b", re.I
)
_SELF_CALENDAR = re.compile(r"\byour (?:exact )?(?:calendar|event)(?: details| content)?\b", re.I)
_SELF_EMAIL = re.compile(
    r"\b(?:your email(?: address)?|email(?: address)? (?:do you use|for you))\b", re.I
)
_SELF_PHONE = re.compile(r"\byour (?:phone|phone number|contact number)\b", re.I)
_SELF_LOCATION = re.compile(
    r"\b(?:your (?:precise )?(?:location|address|gps)|where are you located)\b", re.I
)


def canonicalize_availability(peer_handle: str, message: str) -> str:
    """Convert only an exact selected handle in a direct availability question."""
    if not isinstance(peer_handle, str) or not isinstance(message, str):
        return message
    handle = re.escape(peer_handle)
    patterns = (
        (rf"^(?P<prefix>\s*)(?P<word>when)\s+is\s+@?{handle}\s+free\b(?P<suffix>.*)$", "when are you free"),
        (rf"^(?P<prefix>\s*)(?P<word>is)\s+@?{handle}\s+available\b(?P<suffix>.*)$", "are you available"),
        (rf"^(?P<prefix>\s*)@?{handle}'s\s+availability\b(?P<suffix>.*)$", "your availability"),
    )
    for pattern, replacement in patterns:
        match = re.match(pattern, message, re.I)
        if match is not None:
            first = match.groupdict().get("word", "")
            if first[:1].isupper():
                replacement = replacement[:1].upper() + replacement[1:]
            return f"{match.group('prefix')}{replacement}{match.group('suffix')}"
    return message


def classify(kind: RequestKind | str, purpose: str, message: str) -> tuple[RequestKind, RequestAction, str]:
    """Ignore caller labels and return only bounded, host-trusted values."""
    # Purpose and kind are peer/model-authored labels. They may narrow policy,
    # but can never turn an imperative message into an allowed proposal.
    text = message
    if (
        _CREDENTIAL.search(text)
        or _PRIVATE_CATEGORY.search(text)
        or _THIRD_PARTY_REFERENCE.search(text)
    ):
        return RequestKind.SENSITIVE, RequestAction.CREDENTIALS, "sensitive_request"
    proposal = False
    for clause in re.split(r"[.!?;\n]+", text):
        clause = clause.strip()
        if not clause:
            continue
        proposed = _PROPOSAL.search(clause)
        if proposed is not None:
            proposal = True
            prefix = clause[:proposed.start()]
            remainder = clause[proposed.end():]
            if (
                _MUTATION.search(prefix)
                or _FORWARD.search(prefix)
                or _MUTATION.search(remainder)
                or _FORWARD.search(remainder)
                or re.search(r"\b(?:please|now|go ahead(?: and)?|then)\s+", remainder, re.I)
            ):
                return RequestKind.SENSITIVE, RequestAction.MUTATING_ACTION, "sensitive_request"
            continue
        if _FORWARD.search(clause):
            return RequestKind.SENSITIVE, RequestAction.THIRD_PARTY_FORWARD, "sensitive_request"
        if _MUTATION.search(clause):
            return RequestKind.SENSITIVE, RequestAction.MUTATING_ACTION, "sensitive_request"
    if proposal:
        return RequestKind.SENSITIVE, RequestAction.COMMITMENT_PROPOSAL, "commitment_proposal"
    if _SENSITIVE.search(text):
        return RequestKind.SENSITIVE, RequestAction.ASK, "sensitive_request"
    if _AVAILABILITY.search(text) or kind == RequestKind.AVAILABILITY:
        return RequestKind.AVAILABILITY, RequestAction.ASK, "availability"
    if kind == RequestKind.SENSITIVE:
        return RequestKind.SENSITIVE, RequestAction.ASK, "sensitive_request"
    return RequestKind.ORDINARY_MESSAGE, RequestAction.ASK, "question"


def disclosure_scope(purpose: str, message: str, action: RequestAction) -> str:
    """Return the sole category a confirmation may authorize; all other private data stays denied."""
    text = message
    if action == RequestAction.COMMITMENT_PROPOSAL:
        return "commitment_proposal"
    if _AVAILABILITY.search(message) and _SELF_AVAILABILITY.search(text):
        return "availability"
    if re.search(r"\b(?:raw (?:file|message|memory)|private (?:content|message))\b", text, re.I):
        return "none"
    if re.search(r"\b(?:exact (?:calendar|event)|calendar (?:details|content))\b", text, re.I) and _SELF_CALENDAR.search(text): return "calendar_detail"
    if re.search(r"\bphone\b", text, re.I) and _SELF_PHONE.search(text): return "contact_phone"
    if re.search(r"\bemail\b", text, re.I) and _SELF_EMAIL.search(text): return "contact_email"
    if re.search(r"\b(?:precise (?:location|address)|gps)\b", text, re.I) and _SELF_LOCATION.search(text): return "precise_location"
    return "none"
