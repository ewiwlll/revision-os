"""Ledger d'usage et prix — porté de BioMIA/scripts/ingest_course.py (record_or_usage, prix par modèle).

Schéma de ligne identique à `gemini_usage.jsonl` (ts, provider, model, chunk, tokens,
costUSD, prices) : les coûts mesurés en beta serviront à fixer le prix des crédits.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

AUDIO_TOKENS_PER_SECOND = 32          # estimation doc Gemini (audio -> tokens)
AUDIO_TOKENS_EST_PER_SEC = 25.0       # estimation doc Gemini
OUTPUT_TOKENS_EST_PER_SEC = 4.5       # calibré sur mesures (français parlé)

# USD / million de tokens — clés = préfixes de modèle (le préfixe fournisseur est ignoré).
PRICES_BY_MODEL = {
    "gemini-3.8-flash": {"text": 0.75, "audio": 0.75, "output": 3.75},
    "gemini-3.7-flash": {"text": 0.75, "audio": 0.75, "output": 3.75},
    "gemini-3.5-flash-lite": {"text": 0.30, "audio": 0.30, "output": 2.50},
    "gemini-3.5-transcribe": {"text": 2.00, "audio": 2.00, "output": 12.00},
    "deepseek-v4.1-flash": {"text": 0.15, "audio": 0.15, "output": 0.60},
    "deepseek-v4-pro": {"text": 0.36, "audio": 0.36, "output": 0.72},
    "deepseek-v4-flash": {"text": 0.091, "audio": 0.091, "output": 0.182},
}
DEFAULT_PRICES = {"text": 0.75, "audio": 0.75, "output": 3.75}


def prices_for(model: str | None) -> dict:
    key = (model or "").lower().split("/")[-1]
    for name, prices in PRICES_BY_MODEL.items():
        if key.startswith(name):
            return dict(prices)
    return dict(DEFAULT_PRICES)


def estimate_cost_usd(duration_sec: float, model: str | None = None) -> float:
    """Coût estimé d'une transcription (tokens audio + sortie), selon le modèle."""
    prices = prices_for(model)
    return (
        duration_sec * AUDIO_TOKENS_EST_PER_SEC * prices["audio"]
        + duration_sec * OUTPUT_TOKENS_EST_PER_SEC * prices["output"]
    ) / 1_000_000


def cost_for(*, prompt_tokens: int, output_tokens: int, audio_seconds: float, prices: dict):
    """Retourne (costUSD, audio_tokens, text_tokens) selon la répartition estimation/doc."""
    audio_tokens = min(int(audio_seconds * AUDIO_TOKENS_PER_SECOND), prompt_tokens)
    text_tokens = max(0, prompt_tokens - audio_tokens)
    cost = (
        audio_tokens * prices["audio"]
        + text_tokens * prices["text"]
        + output_tokens * prices["output"]
    ) / 1_000_000
    return cost, audio_tokens, text_tokens


class UsageLedger:
    """Journal JSONL — une ligne par appel modèle."""

    def __init__(self, path):
        self.path = Path(path)

    def append(self, entry: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def spent_since(self, ts_iso: str) -> float:
        if not self.path.exists():
            return 0.0
        total = 0.0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(entry.get("ts", "")) >= ts_iso:
                total += float(entry.get("costUSD", 0) or 0)
        return total


def usage_entry(*, provider: str, model: str, chunk: str, start_sec: float, end_sec: float,
                usage: dict | None, prices: dict) -> dict:
    prompt_tokens = int((usage or {}).get("prompt_tokens", 0) or 0)
    output_tokens = int((usage or {}).get("completion_tokens", 0) or 0)
    audio_seconds = max(0.0, float(end_sec) - float(start_sec))
    cost, audio_tokens, text_tokens = cost_for(
        prompt_tokens=prompt_tokens, output_tokens=output_tokens,
        audio_seconds=audio_seconds, prices=prices)
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": provider,
        "model": model,
        "chunk": chunk,
        "audioSeconds": round(audio_seconds, 1),
        "promptTokens": prompt_tokens,
        "audioTokens": audio_tokens,
        "textTokens": text_tokens,
        "outputTokens": output_tokens,
        "thinkingTokens": 0,
        "cachedTokens": 0,
        "costUSD": round(cost, 6),
        "prices": dict(prices),
    }
