"""Resolve configured IIC personas without importing the full graph stack."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping, Optional

from tradingagents.personas.loader import Persona, load_persona_from_string


def personas_dir() -> Path:
    """Return the source-tree persona directory for backwards compatibility."""
    return Path(__file__).resolve().parent


def load_packaged_persona(persona_id: str) -> Optional[Persona]:
    """Load one persona from installed package data."""
    resource = files("tradingagents.personas").joinpath(f"{persona_id}.yaml")
    if not resource.is_file():
        return None
    return load_persona_from_string(resource.read_text(encoding="utf-8"))


def load_persona_from_config(config: Mapping[str, Any]) -> Optional[Persona]:
    persona_id = config.get("persona_id")
    if not persona_id:
        return None
    return load_packaged_persona(str(persona_id))
