"""Images des diaporamas PDF — elles ILLUSTRENT la fiche, elles ne sont JAMAIS une source de contenu.

Règle produit (Émilien, 16/10/2026) : la transcription audio est la source unique de vérité ;
une image ne sert qu'à illustrer une notion déjà expliquée à l'oral, ou à vérifier/corriger.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from .llm import chat
from .usage import prices_for, usage_entry

MIN_SIDE = 150          # px — ignore les logos/puces
MAX_IMAGES = 40         # plafond (coût vision + taille de fiche)
AUDIO_EXTS = (".m4a", ".mp3", ".wav", ".aiff", ".aif", ".ogg", ".flac", ".mp4", ".mpeg", ".webm")

CAPTION_SYSTEM = (
    "Tu décris des schémas de diaporamas universitaires pour indexer une fiche de révision. "
    "Réponds en UNE phrase française (20 mots max), vocabulaire scientifique exact, "
    "sans « cette image », sans méta-commentaire."
)


def pick_audio(sources: list[Path]) -> Path | None:
    for p in sources:
        if p.suffix.lower() in AUDIO_EXTS:
            return p
    return None


def find_pdf(sources: list[Path]) -> Path | None:
    for p in sources:
        if p.suffix.lower() == ".pdf":
            return p
    return None


def extract_pdf_images(pdf_path: Path, out_dir: Path, *, log=print) -> list[dict]:
    """Extrait les images embarquées du PDF → out/images/p{page}-{i}.png + manifest.json."""
    import pymupdf

    images_dir = out_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(str(pdf_path))
    seen: set[int] = set()
    manifest: list[dict] = []
    for pno in range(len(doc)):
        page = doc[pno]
        for i, info in enumerate(page.get_images(full=True)):
            xref = info[0]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                pix = pymupdf.Pixmap(doc, xref)
            except Exception:  # image illisible → suivante
                continue
            if pix.width < MIN_SIDE or pix.height < MIN_SIDE:
                continue
            if pix.n - pix.alpha >= 4:  # CMYK → RGB
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            name = "p{:02d}-{}.png".format(pno + 1, i + 1)
            pix.save(str(images_dir / name))
            manifest.append({"file": name, "page": pno + 1, "w": pix.width, "h": pix.height, "caption": ""})
            if len(manifest) >= MAX_IMAGES:
                break
        if len(manifest) >= MAX_IMAGES:
            break
    (images_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    log("Images PDF : {} extraites de {} ({} pages)".format(len(manifest), pdf_path.name, len(doc)))
    return manifest


def caption_images(images_dir: Path, manifest: list[dict], *, api_key: str, model: str,
                   effort: str = "low", ledger=None, log=print) -> list[dict]:
    """Une légende vision par image (sert à l'IA de fiche pour choisir où illustrer)."""
    for item in manifest:
        if item.get("caption"):
            continue
        path = images_dir / str(item.get("file", ""))
        if not path.is_file():
            continue
        try:
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            messages = [
                {"role": "system", "content": CAPTION_SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": "Décris ce schéma (diaporama, page {}).".format(item.get("page"))},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
                ]},
            ]
            text, usage = chat(api_key, model, messages, effort=effort, log=log)
            item["caption"] = " ".join(text.strip().split("\n")[0].split())[:200]
            if ledger is not None and usage:
                ledger.append(usage_entry(provider="openrouter", model=model, chunk="images",
                                          start_sec=0.0, end_sec=0.0, usage=usage,
                                          prices=prices_for(model)))
        except Exception as exc:  # noqa: BLE001 — une légende ratée n'arrête pas la génération
            log("   légende KO ({}) : {}".format(item.get("file"), str(exc)[:120]))
    (images_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest
