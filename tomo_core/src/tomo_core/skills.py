"""Packaged capability skills used in Tomo system prompts."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources


MEMORY_SKILL_PATH = "skills/memory/SKILL.md"
CRON_JOBS_SKILL_PATH = "skills/cron-jobs/SKILL.md"
VISUAL_EVIDENCE_SKILL_PATH = "skills/visual-evidence/SKILL.md"
TOMO_CONNECTIONS_SKILL_PATH = "skills/tomo-connections/SKILL.md"


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


@lru_cache(maxsize=1)
def load_cron_jobs_skill() -> str:
    """Return the required packaged cron skill, failing closed if unavailable."""
    try:
        content = resources.files("tomo_core").joinpath(*CRON_JOBS_SKILL_PATH.split("/")).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, ModuleNotFoundError) as error:
        raise RuntimeError(f"required capability skill is missing: {CRON_JOBS_SKILL_PATH}") from error
    if not content:
        raise RuntimeError(f"required capability skill is empty: {CRON_JOBS_SKILL_PATH}")
    return content


@lru_cache(maxsize=1)
def load_visual_evidence_skill() -> str:
    """Return the required packaged visual evidence skill, failing closed if unavailable."""
    try:
        content = resources.files("tomo_core").joinpath(*VISUAL_EVIDENCE_SKILL_PATH.split("/")).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, ModuleNotFoundError) as error:
        raise RuntimeError(f"required capability skill is missing: {VISUAL_EVIDENCE_SKILL_PATH}") from error
    if not content:
        raise RuntimeError(f"required capability skill is empty: {VISUAL_EVIDENCE_SKILL_PATH}")
    return content


@lru_cache(maxsize=1)
def load_tomo_connections_skill() -> str:
    """Return the required packaged Tomo connections skill, failing closed if unavailable."""
    try:
        content = resources.files("tomo_core").joinpath(*TOMO_CONNECTIONS_SKILL_PATH.split("/")).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, ModuleNotFoundError) as error:
        raise RuntimeError(f"required capability skill is missing: {TOMO_CONNECTIONS_SKILL_PATH}") from error
    if not content:
        raise RuntimeError(f"required capability skill is empty: {TOMO_CONNECTIONS_SKILL_PATH}")
    return content


def render_capability_skill_index(*, include_visual_evidence: bool = False, include_tomo_connections: bool = False) -> str:
    """Render the compact index and authoritative content for required capability skills."""
    visual = (
        f"visual-evidence: {VISUAL_EVIDENCE_SKILL_PATH}\n"
        "<TOMO_VISUAL_EVIDENCE_SKILL>\n"
        f"{load_visual_evidence_skill()}\n"
        "</TOMO_VISUAL_EVIDENCE_SKILL>\n"
        if include_visual_evidence else ""
    )
    connections = (
        f"tomo-connections: {TOMO_CONNECTIONS_SKILL_PATH}\n"
        "<TOMO_CONNECTIONS_SKILL>\n"
        f"{load_tomo_connections_skill()}\n"
        "</TOMO_CONNECTIONS_SKILL>\n"
        if include_tomo_connections else ""
    )
    return (
        "<TOMO_CAPABILITY_SKILLS>\n"
        f"memory: {MEMORY_SKILL_PATH}\n"
        f"cron-jobs: {CRON_JOBS_SKILL_PATH}\n"
        f"{visual}"
        f"{connections}"
        "<TOMO_MEMORY_SKILL>\n"
        f"{load_memory_skill()}\n"
        "</TOMO_MEMORY_SKILL>\n"
        "<TOMO_CRON_JOBS_SKILL>\n"
        f"{load_cron_jobs_skill()}\n"
        "</TOMO_CRON_JOBS_SKILL>\n"
        "</TOMO_CAPABILITY_SKILLS>"
    )
