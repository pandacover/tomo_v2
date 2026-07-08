from __future__ import annotations

from pathlib import Path

DEFAULT_SOUL = "you are tomo, a warm, conversational personal agent. keep replies plain and natural."


def load_soul(path: str | Path) -> str:
    soul_path = Path(path)
    if not soul_path.exists():
        return DEFAULT_SOUL
    text = soul_path.read_text(encoding="utf-8").strip()
    return text or DEFAULT_SOUL
