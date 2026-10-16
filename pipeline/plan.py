"""Étape « plan du cours » — nouveauté SaaS (absente du pipeline BioMIA d'origine).

Transcription → plan JSON structuré (sections, notions, plages horaires) : il arme l'écran
« plan éditable » où l'utilisateur valide/corrige AVANT la génération (contrôle qualité + coût).
"""
from __future__ import annotations

import re

from .errors import IngestionError
from .llm import chat, parse_json
from .text import format_time
from .usage import prices_for, usage_entry

PLAN_SYSTEM = (
    "Tu es un architecte pédagogique spécialisé dans les cours universitaires francophones. "
    "À partir d'une transcription horodatée, tu produis le PLAN du cours : les étapes dans "
    "l'ordre chronologique, avec leurs notions clés. Le plan doit être FIDÈLE à la transcription "
    "(aucune invention, aucun contenu absent) et servir de sommaire de révision."
)

# Repère [MM:SS] ou [HH:MM:SS] (brackets optionnels) en début de ligne de transcription.
LINE_TS_RE = re.compile(r"^\s*\[?(\d{1,2}):(\d{2})(?::(\d{2}))?\]?")
_WORD_RE = re.compile(r"\w{5,}", re.UNICODE)


def _validate(data) -> dict:
    if not isinstance(data, dict):
        raise IngestionError("Plan invalide : objet JSON attendu.")
    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        raise IngestionError("Plan invalide : aucune section.")
    clean_sections = []
    for i, s in enumerate(sections):
        if not isinstance(s, dict) or not str(s.get("titre") or "").strip():
            raise IngestionError("Plan invalide : section {} sans titre.".format(i + 1))
        notions = s.get("notions") or []
        if not isinstance(notions, list):
            notions = [str(notions)]
        clean_sections.append({
            "titre": str(s.get("titre")).strip(),
            "resume": str(s.get("resume") or "").strip(),
            "notions": [str(n).strip() for n in notions if str(n).strip()],
            "debut": str(s.get("debut") or "").strip(),
            "fin": str(s.get("fin") or "").strip(),
        })
    themes = data.get("themes") or []
    if not isinstance(themes, list):
        themes = [str(themes)]
    return {
        "titre": str(data.get("titre") or "").strip(),
        "resume": str(data.get("resume") or "").strip(),
        "sections": clean_sections,
        "themes": [str(t).strip() for t in themes if str(t).strip()],
    }


def _ts_seconds(match: re.Match) -> int:
    """Secondes depuis un match de LINE_TS_RE (MM:SS ou HH:MM:SS)."""
    a, b, c = match.group(1), match.group(2), match.group(3)
    if c is not None:
        return int(a) * 3600 + int(b) * 60 + int(c)
    return int(a) * 60 + int(b)


def _transcript_lines(transcript: str) -> list[tuple[int, str]]:
    """Lignes horodatées de la transcription -> [(secondes, texte normalisé)]."""
    out: list[tuple[int, str]] = []
    for raw in str(transcript).splitlines():
        m = LINE_TS_RE.match(raw)
        if not m:
            continue
        out.append((_ts_seconds(m), raw[m.end():].lower()))
    return out


def _keywords(text) -> set[str]:
    return set(_WORD_RE.findall(str(text).lower()))


def _fix_timecodes(plan: dict, transcript: str) -> dict:
    """Recale les debut/fin restés à 00:00 sur les horodatages RÉELS de la transcription.

    Passe déterministe et silencieuse : si les horodatages ne sont pas détectables (aucune
    ligne horodatée, aucun repère > 0), le plan est renvoyé inchangé.
    """
    lines = _transcript_lines(transcript)
    sections = plan.get("sections") if isinstance(plan, dict) else None
    if not lines or not isinstance(sections, list) or not sections:
        return plan
    if lines[-1][0] <= 0:
        return plan

    line_tokens = [set(_WORD_RE.findall(body)) for _, body in lines]

    # Position de la première ligne de contenu de chaque section (recherche séquentielle).
    starts: list[int | None] = []
    cursor = 0
    for s in sections:
        kws = _keywords(s.get("titre")) | _keywords(s.get("resume"))
        for n in s.get("notions") or []:
            kws |= _keywords(n)
        found = None
        for idx in range(cursor, len(lines)):
            if kws and kws & line_tokens[idx]:
                found = idx
                break
        starts.append(found)
        if found is not None:
            cursor = found + 1

    last_sec = lines[-1][0]
    fixed = []
    for i, s in enumerate(sections):
        section = dict(s)
        debut = str(s.get("debut") or "").strip()
        if debut in ("", "00:00", "0:00", "00:00:00") and starts[i] is not None:
            start_sec = lines[starts[i]][0]
            nxt = starts[i + 1] if i + 1 < len(starts) else None
            if nxt is not None and nxt > starts[i]:
                end_sec = lines[nxt - 1][0]
            elif i + 1 < len(sections) and starts[i + 1] is None:
                # Section suivante non localisée : on ne recale pas la fin (borne inconnue).
                end_sec = last_sec
            else:
                end_sec = last_sec
            if end_sec < start_sec:
                end_sec = start_sec
            section["debut"] = format_time(start_sec)
            section["fin"] = format_time(end_sec)
        fixed.append(section)

    out = dict(plan)
    out["sections"] = fixed
    return out


def build_plan(*, transcript_md: str, api_key: str, model: str, title: str | None = None,
               subject: str | None = None, effort: str = "medium", ledger=None, log=print) -> dict:
    """Construit le plan du cours à partir de la transcription. Retourne le plan validé."""
    context = []
    if title:
        context.append("Cours : {}.".format(title))
    if subject:
        context.append("Matière : {}.".format(subject))
    user = (
        ("\n".join(context) + "\n\n" if context else "")
        + "Transcription du cours :\n\n<transcription>\n" + transcript_md + "\n</transcription>\n\n"
        "Produis UNIQUEMENT un objet JSON valide, sans texte autour, de la forme exacte :\n"
        '{\n'
        '  "titre": "titre du cours (affiné si utile)",\n'
        '  "resume": "3-4 phrases de synthèse",\n'
        '  "sections": [\n'
        '    {"titre": "...", "resume": "1-2 phrases", "notions": ["terme 1", "terme 2"], '
        '"debut": "MM:SS", "fin": "MM:SS"}\n'
        '  ],\n'
        '  "themes": ["theme 1", "theme 2"]\n'
        '}\n\n'
        "Contraintes :\n"
        "- 3 à 8 sections, dans l'ordre chronologique de la transcription ;\n"
        "- `debut` et `fin` sont les horodatages RÉELS (MM:SS) du premier et du dernier passage de "
        "la transcription appartenant à la section — reprends-les tels quels depuis la transcription, "
        "ne mets JAMAIS 00:00 si la section démarre plus loin ;\n"
        "- \"notions\" = 3 à 8 termes à connaître par section ;\n"
        "- ne recopie pas la transcription : structure-la ; n'invente rien."
    )
    text, usage = chat(api_key, model, [{"role": "system", "content": PLAN_SYSTEM},
                                        {"role": "user", "content": user}],
                       effort=effort, log=log)
    plan = _fix_timecodes(_validate(parse_json(text)), transcript_md)
    if ledger is not None:
        ledger.append(usage_entry(provider="openrouter", model=model, chunk="plan",
                                  start_sec=0.0, end_sec=0.0, usage=usage,
                                  prices=prices_for(model)))
    log("Plan : {} section(s), {} notion(s)".format(
        len(plan["sections"]), sum(len(s["notions"]) for s in plan["sections"])))
    return plan
