"""Étape génération — fiche + questions + cartes, avec validators (gates) v0.

Doctrine distillée des skills BioMIA (`biomia-course-structuring`, `biomia-questions-engine`) :
- fiche : prose (2-3 phrases) encadrant des puces scannables `- **mot-clé** : …`, le « pourquoi »
  toujours écrit, fidélité stricte à la transcription, zéro méta-phrase sur le support ;
- questions : une question = une vraie question d'examen, énoncé en copie fermée, réponse scannable
  (1-8 puces à mot-clé en gras, 200-1200 caractères), priorité S/A/B/C, grounding fermé.

Les 7 scripts d'audit du repo BioMIA seront portés progressivement en gates complets (SPEC.md §6) ;
ici : audits v0 outillés (support, longueurs, format des puces, densité).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .errors import IngestionError
from .prompt import language_instruction
from .llm import chat, parse_json
from .text import count_words
from .usage import prices_for, usage_entry

FICHE_SYSTEM = (
    "Tu es un rédacteur pédagogique expert pour des cours universitaires francophones. "
    "Tu produis la FICHE DE RÉVISION de référence d'un cours, à partir de sa transcription et de son plan.\n"
    "Règles absolues :\n"
    "1. Fidélité totale : n'écris QUE ce qui figure dans la transcription. Aucune invention, aucun "
    "ajout encyclopédique, aucune anecdote sans valeur de cours.\n"
    "2. Le « pourquoi » est toujours écrit : chaque causalité s'exprime avec son connecteur "
    "(parce que, donc, d'où, ce qui explique, car…).\n"
    "3. Une section = trois temps, dans cet ordre : (a) une PHRASE D'ENTRÉE en prose de 2 à 4 phrases "
    "qui pose le problème ou l'observation (contexte, ce qu'on cherche à comprendre, pourquoi ça compte) ; "
    "(b) des PUCES BARÈME scannables au patron `- **Mot-clé** : information.` — une idée par puce, "
    "information complète, chaque puce commence par un segment en gras ; (c) une PHRASE DE SORTIE en "
    "prose de 2 à 3 phrases qui synthétise ce qu'on retient et fait le lien avec la notion suivante. "
    "La prose explique, les puces récapitulent : JAMAIS de section réduite à une liste de puces.\n"
    "4. Prose riche et explicative : les mécanismes, enchaînements et conséquences s'expliquent en "
    "phrases complètes — un étudiant qui n'a pas suivi le cours doit tout comprendre en lisant la fiche "
    "seule. Interdit : style télégraphique, fragments, éléments juxtaposés sans explication.\n"
    "5. Terme technique défini avant usage ; synonymes déclarés à la première occurrence "
    "(« membrane cytoplasmique » = « membrane plasmique ») ; contradictions arbitrées explicitement.\n"
    "6. Formules mathématiques en KaTeX ($...$) uniquement pour les maths ; unités et grandeurs "
    "en texte simple.\n"
    "7. Zéro méta-commentaire sur le support (« cette diapositive montre… », « le cours liste… ») : "
    "écris les faits, pas la mise en page.\n"
    "8. Administratif hors cours (annonces TD/TP, dates d'examen, organisation, corrections, "
    "logistique) : ne pollue JAMAIS les sections de cours — regroupe-le dans une section finale "
    "`## Infos pratiques` en puces courtes ; s'il n'y a rien d'administratif, omets-la.\n"
    "9. Pièges : uniquement ceux réellement signalés dans la transcription — sinon, aucun.\n"
    "Structure imposée : un titre (#), une intro de 2 phrases, `## Objectifs — À la fin de cette "
    "fiche, tu sauras :` (3 à 6 verbes d'action), une section `## N. Titre (MM:SS→MM:SS)` par "
    "section du plan dans l'ordre (le contenu administratif est déplacé en fin de fiche), "
    "`## Points clés` (4 à 6 puces de synthèse), puis `## Infos pratiques` si nécessaire."
)

QUESTIONS_SYSTEM = (
    "Tu es le générateur de questions de contrôle continu d'un cours universitaire francophone. "
    "À partir de la fiche de révision et de la transcription, tu produis des questions EXACTEMENT "
    "comme un examen les poserait.\n"
    "Règles absolues :\n"
    "1. Grounding fermé : chaque question ne porte que sur du contenu présent dans la fiche ou la "
    "transcription. Zéro étymologie, zéro savoir externe, zéro anecdote.\n"
    "2. Une question = une vraie question d'examen : pas de miettes, pas de découpage artificiel en "
    "sous-questions, pas de question-cadeau.\n"
    "3. Copie fermée : l'énoncé ne contient AUCUN indice, aucune liste, aucune réponse partielle. "
    "Il interroge la notion, jamais le support (jamais « selon le diaporama », « le cours liste »…).\n"
    "4. Réponse scannable : 1 à 8 puces plates, chaque puce commence par un segment en gras "
    "(`- **mot-clé** : information.`), entre 200 et 1200 caractères, zéro puce imbriquée, zéro "
    "méta-commentaire.\n"
    "5. Priorité : `S` = définitions/règles/formules/faits signalés comme importants à l'oral ; "
    "`A` = mécanismes et causalité ; `B` = exemples et applications ; `C` = détails mineurs.\n"
    "6. Vocabulaire scientifique exact de la transcription.\n"
    "7. Couverture : chaque section du plan (hors « Infos pratiques ») doit avoir AU MOINS une question ; "
    "aucune partie du cours ne doit être ignorée.\n"
    "8. Hors sujet administratif : jamais de question sur les annonces TD/TP, les dates, l'organisation, "
    "les corrections ou la logistique — tout ce qui relève de « Infos pratiques » est EXCLU.\n"
    "Réponds UNIQUEMENT avec un objet JSON valide, sans texte autour."
)

SUPPORT_RE = re.compile(
    r"\b(diapo|diapositive|diapos|slide|slides|planche|le cours (liste|donne|présente|présentait))\b",
    re.IGNORECASE)


def generate_fiche(*, transcript_md: str, plan: dict, api_key: str, model: str, output_language: str | None = None,
                   effort: str = "medium", ledger=None, log=print, images: list | None = None) -> str:
    img_block = ""
    if images:
        lines = "\n".join(
            "- images/{} (diapo p. {}) : {}".format(i.get("file", ""), i.get("page", "?"),
                                                    i.get("caption") or "schéma du cours")
            for i in images)
        img_block = (
            "\n\nIllustrations disponibles (images extraites du diaporama du cours) :\n" + lines + "\n\n"
            "Règles d'illustration :\n"
            "- Mets les images qui sont en lien avec l'audio : celles dont la notion est expliquée dans la transcription (même brièvement).\n"
            "- Enlève celles qui ne correspondent pas à ce qui est expliqué (schémas d'autres chapitres, diapos vides, captures sans rapport).\n"
            "- Chaque image sur sa propre ligne, juste après les puces de la notion qu'elle illustre : ![légende courte](images/<fichier>)\n"
            "- Légende : 3 à 8 mots en français décrivant ce que montre le schéma."
        )
    user = (
        "Plan du cours :\n" + json.dumps(plan, ensure_ascii=False, indent=1) + "\n\n"
        "Transcription (source unique de vérité) :\n<transcription>\n" + transcript_md
        + "\n</transcription>\n\n"
        "Rédige la fiche de révision complète en Markdown (pas de bloc de code autour), en suivant "
        "la structure imposée et les sections du plan dans l'ordre. Chaque section garde un volume "
        "proportionné à son temps de cours." + img_block + language_instruction(output_language)
    )
    text, usage = chat(api_key, model, [{"role": "system", "content": FICHE_SYSTEM},
                                        {"role": "user", "content": user}], effort=effort, log=log)
    if ledger is not None:
        ledger.append(usage_entry(provider="openrouter", model=model, chunk="fiche",
                                  start_sec=0.0, end_sec=0.0, usage=usage, prices=prices_for(model)))
    return text.strip() + "\n"


def generate_questions(*, fiche_md: str, transcript_md: str, plan: dict, api_key: str, model: str, output_language: str | None = None,
                       effort: str = "medium", ledger=None, log=print) -> list[dict]:
    words = count_words(fiche_md)
    target = max(6, round(6 * words / 1000))   # proportionnel au cours — plus de plafond à 20
    target = min(target, 40)                   # garde-fou de coût (un cours de 2 h ≈ 30-40 questions)
    sections = [str(s.get("titre") or "") for s in plan.get("sections", [])]
    batches = 2 if target > 24 else 1          # découpe en 2 lots quand c'est long (sortie modèle bornée)
    per_batch = -(-target // batches)
    questions: list[dict] = []
    for gi in range(batches):
        if batches > 1:
            half = (len(sections) + 1) // 2
            group = sections[:half] if gi == 0 else sections[half:]
            focus = ("Concentre-toi sur ces sections du plan : " + " · ".join(group) + "\n\n")
        else:
            focus = ""
        user = (
            "Fiche de révision :\n<fiche>\n" + fiche_md + "\n</fiche>\n\n"
            "Transcription (source) :\n<transcription>\n" + transcript_md + "\n</transcription>\n\n"
            + focus
            + "Produis environ {} questions (bande : 4 à 9 par 1000 mots de fiche). ".format(per_batch)
            + "Réponds UNIQUEMENT en JSON, de la forme exacte :\n"
            '{"questions": [{"theme": "THÈME EN MAJUSCULES", "titre": "libellé bref de la notion", '
            '"enonce": "question en copie fermée", "reponse": "réponse scannable", "priorite": "S"}]}\n'
            "Contraintes : thèmes courts en majuscules ; titres brefs ; énoncés autonomes sans mention "
            "du support ; réponses au format puces à mot-clé en gras."
            + language_instruction(output_language)
        )
        text, usage = chat(api_key, model, [{"role": "system", "content": QUESTIONS_SYSTEM},
                                            {"role": "user", "content": user}], effort=effort, log=log)
        if ledger is not None:
            ledger.append(usage_entry(provider="openrouter", model=model, chunk="questions",
                                      start_sec=0.0, end_sec=0.0, usage=usage, prices=prices_for(model)))
        data = parse_json(text)
        raw = data.get("questions") if isinstance(data, dict) else data
        if not isinstance(raw, list) or not raw:
            raise IngestionError("Questions illisibles dans la réponse du modèle.")
        for q in raw:
            if not isinstance(q, dict):
                continue
            prio = str(q.get("priorite") or "").strip().upper()[:1]
            questions.append({
                "theme": str(q.get("theme") or "").strip(),
                "titre": str(q.get("titre") or "").strip(),
                "enonce": str(q.get("enonce") or "").strip(),
                "reponse": str(q.get("reponse") or "").strip(),
                "priorite": prio if prio in ("S", "A", "B", "C") else "A",
            })
    return questions


def questions_to_cards(questions: list[dict]) -> list[dict]:
    """Une carte par question (front = énoncé, back = réponse) — parité pack ↔ deck."""
    cards = []
    for q in questions:
        cards.append({
            "front": q["enonce"],
            "back": q["reponse"],
            "tags": [q["priorite"], q["theme"]] if q.get("theme") else [q["priorite"]],
            "source": q.get("titre", ""),
        })
    return cards


def audit_outputs(*, fiche_md: str, questions: list[dict], plan: dict) -> dict:
    """Audits v0 (gates) : retourne {"hard": [...], "warn": [...]}."""
    hard: list[str] = []
    warn: list[str] = []

    # --- Fiche ---
    if len(fiche_md) < 500:
        hard.append("Fiche trop courte ({} caractères).".format(len(fiche_md)))
    plan_titles = [s["titre"].lower() for s in plan.get("sections", [])]
    fiche_low = fiche_md.lower()
    missing = [t for t in plan_titles if t.split()[0:2] and t.split()[0] not in fiche_low]
    if missing and len(missing) == len(plan_titles):
        warn.append("Aucun titre de section du plan retrouvé dans la fiche.")
    if not re.search(r"parce que|donc|d'où|ce qui explique|car ", fiche_md, re.IGNORECASE):
        warn.append("Aucun connecteur causal détecté dans la fiche.")
    for m in SUPPORT_RE.finditer(fiche_md):
        warn.append("Méta-phrase support dans la fiche : « {} »".format(m.group(0)))
    if "**" not in fiche_md:
        hard.append("Fiche sans puces à mot-clé en gras.")

    # --- Questions ---
    if len(questions) < 3:
        hard.append("Moins de 3 questions générées.")
    for i, q in enumerate(questions, 1):
        enonce, rep = q.get("enonce", ""), q.get("reponse", "")
        if not enonce or not rep:
            hard.append("Q{} : énoncé ou réponse vide.".format(i))
            continue
        if SUPPORT_RE.search(enonce):
            hard.append("Q{} : l'énoncé parle du support (copie fermée).".format(i))
        if len(rep) > 1500:
            hard.append("Q{} : réponse trop longue ({} caractères, max 1500).".format(i, len(rep)))
        elif len(rep) < 120:
            warn.append("Q{} : réponse très courte ({} caractères) — vérifier le grain.".format(i, len(rep)))
        bullets = [l for l in rep.splitlines() if l.strip().startswith("-")]
        if len(bullets) > 8:
            warn.append("Q{} : {} puces (max 8).".format(i, len(bullets)))
        bold = [b for b in bullets if b.strip().startswith("- **")]
        if bullets and len(bold) * 2 < len(bullets):
            warn.append("Q{} : puces sans mot-clé en gras ({}/{})".format(i, len(bold), len(bullets)))
    words = count_words(fiche_md)
    if words > 0:
        density = len(questions) / (words / 1000)
        if density < 2:
            warn.append("Densité faible : {:.1f} questions / 1000 mots de fiche.".format(density))
        elif density > 12:
            warn.append("Densité élevée : {:.1f} questions / 1000 mots de fiche.".format(density))
    return {"hard": hard, "warn": warn}


def run_generate(*, transcript_md: str, plan: dict, out_dir, api_key: str, model: str, output_language: str | None = None,
                 effort: str = "medium", ledger=None, log=print, images: list | None = None) -> dict:
    """Génère fiche + questions + cartes, écrit les artefacts, puis applique les audits v0.

    Les artefacts sont écrits même en cas d'audit bloquant (inspection) ; l'erreur est levée ensuite.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log("Génération de la fiche…")
    fiche = generate_fiche(transcript_md=transcript_md, plan=plan, api_key=api_key, model=model,
                           output_language=output_language, effort=effort, ledger=ledger, log=log,
                           images=images)
    fiche_words = count_words(fiche)
    (out_dir / "fiche.md").write_text(fiche, encoding="utf-8")
    log("Fiche : {} mots".format(fiche_words))

    log("Génération des questions…")
    questions = generate_questions(fiche_md=fiche, transcript_md=transcript_md, plan=plan,
                                   api_key=api_key, model=model, output_language=output_language,
                                   effort=effort, ledger=ledger, log=log)
    (out_dir / "questions.json").write_text(
        json.dumps({"questions": questions}, ensure_ascii=False, indent=2), encoding="utf-8")
    log("Questions : {}".format(len(questions)))

    cards = questions_to_cards(questions)
    (out_dir / "cards.json").write_text(
        json.dumps({"cards": cards}, ensure_ascii=False, indent=2), encoding="utf-8")

    audit = audit_outputs(fiche_md=fiche, questions=questions, plan=plan)
    for w in audit["warn"]:
        log("⚠️  " + w)
    for h in audit["hard"]:
        log("⛔ " + h)
    result = {"out_dir": str(out_dir), "fiche_words": fiche_words,
              "questions": len(questions), "cards": len(cards),
              "audit": {"hard": len(audit["hard"]), "warn": len(audit["warn"])}}
    if audit["hard"]:
        raise IngestionError("Audits bloquants ({} ) : {}".format(
            len(audit["hard"]), " ; ".join(audit["hard"])))
    log("Audits OK — {} avertissement(s).".format(len(audit["warn"])))
    return result
