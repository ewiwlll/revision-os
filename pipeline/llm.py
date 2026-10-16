"""Client LLM texte OpenRouter — réutilisé par l'étape plan et la génération.

Même politique d'erreur que le client audio : 402/crédits → QuotaAbort ;
429/rate-limit → pauses puis QuotaAbort ; transitoires → 3 essais.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .errors import QuotaAbort

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
REFERER = "https://github.com/revision-saas"
TITLE = "revision-pipeline"
QUOTA_WAIT_SEC = 60
QUOTA_MAX_RETRIES = 3
TRANSIENT_WAIT_SEC = 10
TRANSIENT_MAX_ATTEMPTS = 3


class LLMError(RuntimeError):
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
        raise LLMError("HTTP {} {}".format(exc.code, detail))


def chat(api_key: str, model: str, messages: list[dict], *, effort: str = "medium",
         timeout: int = 300, log=print):
    """Appel chat/completions. Retourne (texte, usage)."""
    if effort not in ("xhigh", "high", "medium", "low", "minimal"):
        effort = "medium"
    payload = {"model": model, "reasoning": {"effort": effort}, "messages": messages}
    quota_hits = 0
    transient_hits = 0
    t0 = time.time()
    while True:
        try:
            status, data = _post(api_key, payload, timeout=timeout)
            if status != 200:
                raise LLMError("statut inattendu {}".format(status))
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            text = msg.get("content") or ""
            if not text.strip():
                raise LLMError("réponse vide du modèle")
            usage = data.get("usage") or {}
            log("   LLM {} : {:.1f}s · in={} out={}".format(
                model, time.time() - t0,
                usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?")))
            return text, usage
        except QuotaAbort:
            raise
        except Exception as exc:  # noqa: BLE001 — politique de retry du pipeline
            blob = str(exc).lower()
            if "402" in blob or "credit" in blob:
                raise QuotaAbort("Crédits OpenRouter épuisés : recharge puis relance.")
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
                    raise LLMError("abandon après {} essais : {}".format(
                        TRANSIENT_MAX_ATTEMPTS, str(exc)[:200]))
                time.sleep(TRANSIENT_WAIT_SEC)


def parse_json(text: str):
    """Extrait un objet JSON d'une réponse modèle (tolère les fences ```json)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        return json.loads(cleaned[start:end + 1])
    raise LLMError("JSON illisible dans la réponse du modèle.")
