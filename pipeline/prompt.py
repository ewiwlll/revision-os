"""Prompt de transcription — dérivé de BioMIA/scripts/ingest_course.py (build_prompt).

Généralisé pour le multi-utilisateur (plus de référence à un campus) ; les règles de
qualité — mot-à-mot, timestamps absolus, lexique strictement orthographique — sont
conservées à l'identique.
"""
from __future__ import annotations

from .text import format_time


_LANG_NAMES = {"fr": "français", "en": "anglais", "es": "espagnol", "de": "allemand", "it": "italien",
               "pt": "portugais", "nl": "néerlandais", "zh": "chinois", "ar": "arabe"}


def language_instruction(output_language: str | None) -> str:
    """Instruction injectée quand on veut une langue de sortie différente de celle du cours.

    Par défaut (None) : on suit la langue de l'audio — c'est la transcription qui fait foi.
    """
    if not output_language:
        return ""
    lang = _LANG_NAMES.get(output_language.strip().lower(), output_language.strip())
    return (
        "\n\nLangue de rédaction : {lang}. Traduis la prose, mais GARDE les termes techniques du "
        "cours tels qu'ils ont été prononcés, en ajoutant la traduction entre parenthèses à la "
        "première occurrence. Traduis aussi les intitulés de structure (Objectifs, Infos pratiques…). "
        "Les citations du professeur restent dans leur langue d'origine.".format(lang=lang)
    )


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
        "Tu transcris un enregistrement de cours universitaire.\n"
        + ("\n".join(context) + "\n" if context else "")
        + "Segment : {label}, créneau ABSOLU [{start} -> {end}] d'un enregistrement de {total}.\n"
        "\n"
        "CONSIGNES STRICTES :\n"
        "1. Transcription mot-à-mot intégrale DANS LA LANGUE D'ORIGINE, sans jamais traduire : chaque "
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
