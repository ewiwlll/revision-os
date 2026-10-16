"""Prompt de transcription — dérivé de BioMIA/scripts/ingest_course.py (build_prompt).

Généralisé pour le multi-utilisateur (plus de référence à un campus) ; les règles de
qualité — mot-à-mot, timestamps absolus, lexique strictement orthographique — sont
conservées à l'identique.
"""
from __future__ import annotations

from .text import format_time


def build_transcription_prompt(*, label: str, start_sec: float, end_sec: float, total_sec: float,
                               title: str, subject: str | None = None, date_fr: str | None = None,
                               teacher: str | None = None, lexicon: str | None = None) -> str:
    context = []
    if subject:
        context.append("Matière : {}.".format(subject))
    if title:
        context.append("Séance : « {} ».".format(title))
    if date_fr:
        context.append("Date : {}.".format(date_fr))
    if teacher:
        context.append("Enseignant : {}.".format(teacher))

    base = (
        "Tu transcris un enregistrement de cours universitaire en français.\n"
        + ("\n".join(context) + "\n" if context else "")
        + "Segment : {label}, créneau ABSOLU [{start} -> {end}] d'un enregistrement de {total}.\n"
        "\n"
        "CONSIGNES STRICTES :\n"
        "1. Transcription mot-à-mot intégrale en français : chaque phrase, chaque explication dictée "
        "ou écrite au tableau, chaque définition, formule, valeur numérique, unité et exemple, les "
        "anecdotes et digressions de l'enseignant (elles contiennent les pièges et les "
        "mnémotechniques), ainsi que les questions d'étudiants identifiables "
        "([Étudiant : ...] / [Étudiante : ...]). Ne résume rien, ne coupe rien, n'invente rien.\n"
        "2. Orthographe exacte du vocabulaire scientifique, des noms propres, des unités et des "
        "notations du cours. En cas de doute sur un terme, écris-le entre crochets : [terme ?].\n"
        "3. Insère des repères temporels ABSOLUS (temps écoulé depuis le début de l'enregistrement "
        "COMPLET, pas depuis le début du segment) au format [MM:SS] ou [HH:MM:SS], toutes les 2 à 3 "
        "minutes, en commençant par [{start}].\n"
        "4. Réponds UNIQUEMENT avec le texte transcrit : zéro métadonnée, zéro en-tête, zéro "
        "commentaire, zéro résumé."
    )
    prompt = base.format(label=label, start=format_time(start_sec), end=format_time(end_sec),
                         total=format_time(total_sec))
    if lexicon:
        prompt += (
            "\n5. VOCABULAIRE DU DIAPORAMA — usage STRICTEMENT orthographique : ce lexique sert "
            "uniquement à écrire correctement les termes que tu ENTENDS. ⛔ Tu ne dois ajouter, "
            "relier ou compléter AUCUN contenu à partir de ce lexique : si un terme n'est pas "
            "clairement prononcé dans l'audio, ne l'écris pas ; en cas de doute, garde ce qui est "
            "entendu, même si le terme n'est pas dans la liste.\n"
            "{}\n".format(lexicon)
        )
    return prompt
