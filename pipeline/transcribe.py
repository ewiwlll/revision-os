"""Client de transcription OpenRouter — porté de BioMIA/scripts/openrouter_audio.py.

API OpenAI-compatible (chat/completions + input_audio base64). Stdlib uniquement.
La clé n'est JAMAIS affichée : elle transite en mémoire vers le header Authorization,
les logs n'impriment que statut, tokens et coûts.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from .errors import QuotaAbort

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
REFERER = "https://github.com/revision-saas"
TITLE = "revision-pipeline"
QUOTA_WAIT_SEC = 60
QUOTA_MAX_RETRIES = 3
TRANSIENT_WAIT_SEC = 10
TRANSIENT_MAX_ATTEMPTS = 3


class OpenRouterError(RuntimeError):
    pass


def _post(api_key: str, payload: dict, timeout: int = 300):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "HTTP-Referer": REFERER,
            "X-Title": TITLE,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")[:300]
        except Exception:
            detail = ""
        raise OpenRouterError("HTTP {} {}".format(exc.code, detail))


def transcribe_chunk(api_key: str, model: str, chunk_audio, prompt: str, *,
                     effort: str = "medium", timeout: int = 300, log=print):
    """Transcrit un chunk audio via OpenRouter. Retourne (texte, usage)."""
    if effort not in ("xhigh", "high", "medium", "low", "minimal"):
        effort = "medium"
    chunk_audio = Path(chunk_audio)
    raw = chunk_audio.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    payload = {
        "model": model,
        "reasoning": {"effort": effort},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "input_audio", "input_audio": {"data": b64, "format": "m4a"}},
                ],
            }
        ],
    }
    quota_hits = 0
    transient_hits = 0
    size_mb = len(raw) / 1e6
    t0 = time.time()
    while True:
        try:
            status, data = _post(api_key, payload, timeout=timeout)
            if status != 200:
                raise OpenRouterError("statut inattendu {}".format(status))
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            text = msg.get("content") or ""
            if not text.strip():
                raise OpenRouterError("réponse vide du modèle")
            usage = data.get("usage") or {}
            log("   OpenRouter {} : {:.1f} Mo audio en {:.1f}s · in={} out={}".format(
                model, size_mb, time.time() - t0,
                usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?")
            ))
            return text, usage
        except QuotaAbort:
            raise
        except Exception as exc:  # noqa: BLE001 — politique de retry du pipeline
            blob = str(exc).lower()
            if "402" in blob or "credit" in blob:
                raise QuotaAbort(
                    "Crédits OpenRouter épuisés : recharge puis relance (la reprise réutilise les caches)."
                )
            if "429" in blob or ("rate" in blob and "limit" in blob):
                quota_hits += 1
                if quota_hits > QUOTA_MAX_RETRIES:
                    raise QuotaAbort(str(exc)[:200])
                log("   Quota OpenRouter ({}/{}) — pause {} s…".format(
                    quota_hits, QUOTA_MAX_RETRIES, QUOTA_WAIT_SEC))
                time.sleep(QUOTA_WAIT_SEC)
            else:
                transient_hits += 1
                if transient_hits > TRANSIENT_MAX_ATTEMPTS:
                    raise OpenRouterError("abandon après {} essais : {}".format(
                        TRANSIENT_MAX_ATTEMPTS, str(exc)[:200]))
                time.sleep(TRANSIENT_WAIT_SEC)
