"""Packaged capability skills used in Tomo system prompts."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources


MEMORY_SKILL_PATH = "skills/memory/SKILL.md"


@lru_cache(maxsize=1)
def load_memory_skill() -> str:
    """Return the required packaged memory skill, failing closed if it is unavailable."""
    try:
        content = resources.files("tomo_core").joinpath(*MEMORY_SKILL_PATH.split("/")).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, ModuleNotFoundError) as error:
        raise RuntimeError(f"required capability skill is missing: {MEMORY_SKILL_PATH}") from error
    if not content:
        raise RuntimeError(f"required capability skill is empty: {MEMORY_SKILL_PATH}")
    return content


def render_capability_skill_index() -> str:
    """Render the compact index and authoritative content for required capability skills."""
    return (
        "<TOMO_CAPABILITY_SKILLS>\n"
        f"memory: {MEMORY_SKILL_PATH}\n"
        "<TOMO_MEMORY_SKILL>\n"
        f"{load_memory_skill()}\n"
        "</TOMO_MEMORY_SKILL>\n"
        "</TOMO_CAPABILITY_SKILLS>"
    )
