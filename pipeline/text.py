"""Utilitaires texte purs — portés fidèlement de BioMIA/scripts/ingest_course.py."""
from __future__ import annotations

import difflib
import re

TS_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]")


def format_time(seconds: float) -> str:
    """Secondes -> MM:SS (ou HH:MM:SS au-delà d'une heure)."""
    total = int(max(0, round(float(seconds))))
    m, s = divmod(total, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return "{:02d}:{:02d}:{:02d}".format(h, m, s)
    return "{:02d}:{:02d}".format(m, s)


def _ts_to_seconds(match: re.Match) -> int:
    a, b, c = match.group(1), match.group(2), match.group(3)
    if c is None:
        return int(a) * 60 + int(b)
    return int(a) * 3600 + int(b) * 60 + int(c)


def realign_timestamps(text: str, chunk_start_sec: float) -> tuple[str, bool]:
    """Filet de sécurité : si TOUS les repères sont antérieurs au début du chunk,
    le modèle les a comptés depuis le début du segment -> on les décale.
    Conservateur : on ne touche à rien dans le doute.
    """
    stamps = [_ts_to_seconds(m) for m in TS_RE.finditer(text)]
    if not stamps or chunk_start_sec <= 60:
        return text, False
    if max(stamps) >= chunk_start_sec - 60:
        return text, False
    shifted = TS_RE.sub(
        lambda m: "[{}]".format(format_time(_ts_to_seconds(m) + chunk_start_sec)), text
    )
    return shifted, True


def ensure_leading_timestamp(text: str, chunk_start_sec: float) -> str:
    """Garantit qu'un chunk commence bien par [MM:SS]."""
    stripped = text.lstrip()
    if stripped.startswith("[") and TS_RE.match(stripped):
        return text
    return "[{}] {}".format(format_time(chunk_start_sec), stripped)


def _lexicon_tokens(text: str) -> list[str]:
    return re.findall(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ'’\-]+", text.lower())


def strip_overlap_prefix(previous_text: str, next_text: str, min_words: int = 3,
                         window: int = 80, threshold: float = 0.8) -> tuple[str, int]:
    """Retire du début de next_text le chevauchement déjà présent à la fin de previous_text.

    Les chunks partagent `overlap` secondes : sans ce nettoyage, la phrase charnière
    apparaît deux fois dans le transcript assemblé. On cherche le plus long suffixe
    du précédent qui réapparaît au début du suivant (tolérance ASR via difflib),
    en acceptant un petit en-tête parasite avant le doublon.
    Retourne (texte_nettoyé, nombre_de_mots_retirés).
    """
    prev_tokens = _lexicon_tokens(previous_text)[-window:]
    next_tokens = _lexicon_tokens(next_text)[:window]
    if len(prev_tokens) < min_words or len(next_tokens) < min_words:
        return next_text, 0
    best = None  # (score, taille, position de départ dans next)
    for start_pos in range(0, min(30, len(next_tokens) - min_words + 1)):
        max_size = min(len(prev_tokens), len(next_tokens) - start_pos, 40)
        for size in range(max_size, min_words - 1, -1):
            candidate = next_tokens[start_pos:start_pos + size]
            ratio = difflib.SequenceMatcher(None, prev_tokens[-size:], candidate).ratio()
            if ratio >= threshold:
                score = (size, -start_pos)
                if best is None or score > best[0]:
                    best = (score, size, start_pos)
                break
    if best is None:
        return next_text, 0
    _, size, start_pos = best
    end_token_index = start_pos + size
    cut = 0
    for count, match in enumerate(re.finditer(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ'’\-]+", next_text), start=1):
        if count == end_token_index:
            cut = match.end()
            break
    cleaned = next_text[cut:].lstrip(" .,;:!?—–-\n\t")
    return cleaned, size


def count_words(text: str) -> int:
    return len([w for w in str(text).split() if w.strip()])
