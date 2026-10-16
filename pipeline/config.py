"""Configuration du worker (variables d'environnement)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    openrouter_api_key: str | None = None
    transcribe_model: str = "google/gemini-3.8-flash"
    gen_model: str = "deepseek/deepseek-v4.1-flash"
    reasoning_effort: str = "medium"
    chunk_minutes: float = 17.0
    chunk_min: float = 9.0
    chunk_max: float = 18.0
    overlap_sec: float = 3.0
    work_dir: Path = field(default_factory=lambda: Path("work"))

    @classmethod
    def from_env(cls) -> "Config":
        effort = os.environ.get("REASONING_EFFORT", "medium").strip().lower()
        if effort not in ("xhigh", "high", "medium", "low", "minimal"):
            effort = "medium"
        return cls(
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY"),
            transcribe_model=os.environ.get("TRANSCRIBE_MODEL", "google/gemini-3.8-flash"),
            gen_model=os.environ.get("GEN_MODEL", "deepseek/deepseek-v4.1-flash"),
            reasoning_effort=effort,
            work_dir=Path(os.environ.get("LEDGER_DIR", "work")),
        )
