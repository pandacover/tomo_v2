"""Validation boundary for direct user personal-data governance."""
from __future__ import annotations

import re

from .personal_data import (MemoryControl, MemoryGovernanceControl,
                            MemoryGovernanceResult, OwnerSettingControl,
                            PendingMemoryActionControl)


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"\S+", text.casefold()))


class MemoryGovernanceService:
    """Applies only controls whose quoted intent appears in this user burst."""

    def __init__(self, repository) -> None:
        self._repository = repository

    def apply(
        self,
        owner_id: str,
        session_key: str,
        user_burst: str,
        control: MemoryControl,
    ) -> MemoryGovernanceResult:
        if not isinstance(control, (MemoryGovernanceControl, PendingMemoryActionControl, OwnerSettingControl)):
            return MemoryGovernanceResult("rejected")
        excerpt = _normalized(control.user_intent_excerpt)
        normalized_burst = _normalized(user_burst)
        # Spaces make this an exact normalized phrase match, rather than an
        # unsafe character-substring match (for example, "delete" in a word).
        if not excerpt or f" {excerpt} " not in f" {normalized_burst} ":
            return MemoryGovernanceResult("rejected")
        return self._repository.apply_user_memory_control(owner_id, session_key, control)
