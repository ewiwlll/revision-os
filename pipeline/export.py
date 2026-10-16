"""Export Anki — construit un .apkg (genanki) depuis un cards.json."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import genanki

from .errors import IngestionError

MODEL_ID = 1741100001  # namespace fixe « Revision SaaS »


def build_apkg(*, cards_path, out_path, deck_name: str, course_id: str) -> dict:
    """Écrit un deck .apkg (modèle Basic Front/Back) à partir d'un cards.json."""
    data = json.loads(Path(cards_path).read_text(encoding="utf-8"))
    cards = data.get("cards") or []
    if not cards:
        raise IngestionError("cards.json vide — rien à exporter.")
    model = genanki.Model(
        MODEL_ID,
        "Revision — Basic",
        fields=[{"name": "Front"}, {"name": "Back"}],
        templates=[{
            "name": "Carte 1",
            "qfmt": "{{Front}}",
            "afmt": "{{FrontSide}}<hr id=answer>{{Back}}",
        }],
    )
    deck_id = int(hashlib.sha1(course_id.encode("utf-8")).hexdigest()[:12], 16)
    deck = genanki.Deck(deck_id, deck_name)
    for c in cards:
        front = str(c.get("front") or "").strip()
        back = str(c.get("back") or "").strip()
        if front and back:
            deck.add_note(genanki.Note(model=model, fields=[front, back]))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    genanki.Package(deck).write_to_file(str(out_path))
    return {"cards": len(deck.notes), "apkg": str(out_path), "bytes": out_path.stat().st_size}
