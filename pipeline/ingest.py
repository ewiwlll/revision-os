"""Orchestration d'ingestion audio → transcript — port de BioMIA/scripts/ingest_course.py (run_ingestion).

Adapté multi-utilisateur : pas de vault/index/verrou fichier ; la reprise repose sur le cache de
chunks (`part{N}_*.txt` > 500 octets) — un chunk déjà transcrit n'est JAMAIS refait. La progression
est remontée par callback (mis à jour en DB côté SaaS).
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .chunker import compute_segments, extract_and_filter_chunk, get_audio_duration
from .errors import IngestionError, QuotaAbort
from .prompt import build_transcription_prompt
from .text import (count_words, ensure_leading_timestamp, format_time, realign_timestamps,
                   strip_overlap_prefix)
from .transcribe import transcribe_chunk
from .usage import UsageLedger, estimate_cost_usd, prices_for, usage_entry

CACHE_MIN_BYTES = 500


def list_chunk_files(chunks_dir: Path, part_index: int) -> list[Path]:
    """Tous les fichiers partN_*.txt présents pour ce chunk (cache candidats)."""
    if not chunks_dir.exists():
        return []
    matches = [p for p in chunks_dir.glob("part{}_*.txt".format(part_index)) if p.is_file()]
    matches.sort(key=lambda p: (p.stat().st_mtime, p.name))
    return matches


def find_cached_chunk(chunks_dir: Path, part_index: int, start: float | None = None,
                      end: float | None = None) -> Path | None:
    """Cache valide (> 500 octets) pour ce chunk.

    Avec `start`/`end` fournis : correspondance EXACTE `partN_{start}_{end}.txt` (les paramètres
    de découpe peuvent différer d'un cours à l'autre — pas de faux hit inter-paramètres).
    Sans bornes : repli sur `partN_*.txt` (comportement historique BioMIA).
    """
    if start is not None and end is not None:
        exact = chunks_dir / "part{}_{}_{}.txt".format(part_index, int(start), int(end))
        if exact.is_file() and exact.stat().st_size > CACHE_MIN_BYTES:
            return exact
        return None
    matches = [p for p in list_chunk_files(chunks_dir, part_index) if p.stat().st_size > CACHE_MIN_BYTES]
    return matches[-1] if matches else None


def build_transcript_header(*, title: str, subject: str | None = None, date_fr: str | None = None,
                            teacher: str | None = None, audio_name: str = "", duration_sec: float = 0.0,
                            segments: list | None = None, overlap_sec: float = 3.0, model: str = "",
                            words: int = 0) -> str:
    lines = [
        "# Transcription — {}".format(title),
        "",
        "**Durée :** {}".format(format_time(duration_sec)),
    ]
    if subject:
        lines.append("**Matière :** {}".format(subject))
    if date_fr:
        lines.append("**Date :** {}".format(date_fr))
    if teacher:
        lines.append("**Enseignant :** {}".format(teacher))
    if segments:
        spans = [e - s for _, s, e in segments]
        lines.append("**Découpage :** {} chunk(s) de {} à {} (recouvrement {:.0f} s)".format(
            len(segments), format_time(min(spans)), format_time(max(spans)), overlap_sec))
    lines.append("**Traitement acoustique :** FFmpeg highpass=f=80,loudnorm (16 kHz mono, AAC 64 kb/s)")
    lines.append("**Modèle d'ingestion :** {}".format(model))
    lines.append("**Volume :** {} mots".format(words))
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def run_ingest(*, audio_path, work_dir, out_path, model: str, api_key: str, effort: str = "medium",
               concurrency: int = 3, chunk_minutes: float = 17.0, chunk_min: float = 9.0,
               chunk_max: float = 18.0, overlap_sec: float = 3.0, title: str = "Cours",
               subject: str | None = None, date_fr: str | None = None, teacher: str | None = None,
               max_cost_usd: float | None = None, ledger: UsageLedger | None = None,
               progress=None, log=print) -> dict:
    """Ingère un audio complet : chunks → transcription (parallèle) → cache → assemblage.

    Retourne {duration, chunks, cached, words, out_path, elapsed}.
    QuotaAbort remonte tel quel : les chunks déjà transcrits restent en cache pour la reprise.
    """
    audio_path = Path(audio_path)
    work_dir = Path(work_dir)
    duration = get_audio_duration(audio_path)
    estimate = estimate_cost_usd(duration, model)
    log("Audio : {} — {} · coût estimé ≈ ${:.3f}".format(audio_path.name, format_time(duration), estimate))
    if max_cost_usd is not None and estimate > max_cost_usd:
        raise IngestionError(
            "Coût estimé ${:.3f} > plafond ${:.3f} pour {:.1f} min d'audio.".format(
                estimate, max_cost_usd, duration / 60))

    segments = compute_segments(duration, chunk_minutes, chunk_min, chunk_max, overlap_sec)
    chunks_dir = work_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    cached = {idx: find_cached_chunk(chunks_dir, idx, start, end) for idx, start, end in segments}
    total = len(segments)
    cached_n = sum(1 for v in cached.values() if v)
    log("{} chunk(s) — {} en cache, {} à transcrire (parallèle: {})".format(
        total, cached_n, total - cached_n, max(1, min(concurrency, total))))
    if progress:
        progress(cached_n, total, "{} chunk(s) à transcrire".format(total - cached_n))

    def process_segment(idx: int, start: float, end: float, label: str):
        hit = cached.get(idx)
        if hit is not None:
            text = hit.read_text(encoding="utf-8")
            log("{} — repris du cache « {} » ({} mots)".format(label, hit.name, count_words(text)))
            return start, text, True
        chunk_audio = chunks_dir / "part{}_{}_{}.m4a".format(idx, int(start), int(end))
        prompt = build_transcription_prompt(
            label=label, start_sec=start, end_sec=end, total_sec=duration,
            title=title, subject=subject, date_fr=date_fr, teacher=teacher)
        try:
            extract_and_filter_chunk(audio_path, start, end, chunk_audio)
            text, usage = transcribe_chunk(api_key, model, chunk_audio, prompt, effort=effort, log=log)
        finally:
            if chunk_audio.exists():
                chunk_audio.unlink()
        if ledger is not None:
            ledger.append(usage_entry(provider="openrouter", model=model, chunk=label,
                                      start_sec=start, end_sec=end, usage=usage,
                                      prices=prices_for(model)))
        text, shifted = realign_timestamps(text, start)
        if shifted:
            log("   Repères relatifs détectés → recalés sur le début de l'enregistrement.")
        text = ensure_leading_timestamp(text, start)
        (chunks_dir / "part{}_{}_{}.txt".format(idx, int(start), int(end))).write_text(text, encoding="utf-8")
        log("{} — {} mots".format(label, count_words(text)))
        return start, text, False

    texts_by_idx: dict[float, str] = {}
    quota_error = None
    done = 0
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, total))) as executor:
        futures = {}
        for idx, start, end in segments:
            label = "[{}/{}] chunk {}→{}".format(idx, total, format_time(start), format_time(end))
            futures[executor.submit(process_segment, idx, start, end, label)] = idx
        for future in as_completed(futures):
            try:
                start, text, _from_cache = future.result()
            except QuotaAbort as exc:
                quota_error = exc
                for pending in futures:
                    pending.cancel()
                break
            texts_by_idx[start] = text
            done += 1
            if progress:
                progress(done, total, "{} chunk(s) traité(s)".format(done))
    if quota_error is not None:
        raise quota_error

    texts = [(s, texts_by_idx[s]) for s in sorted(texts_by_idx)]
    if len(texts) != total:
        raise IngestionError("Assemblage impossible : {}/{} chunks disponibles.".format(len(texts), total))

    pieces = []
    previous_text = ""
    for start, text in texts:
        cleaned = text
        if previous_text:
            cleaned, removed = strip_overlap_prefix(previous_text, text)
            if removed:
                log("   Jointure {} : {} mot(s) de chevauchement retirés.".format(
                    format_time(start), removed))
        pieces.append(cleaned)
        previous_text = cleaned
    body = "\n\n---\n\n".join(pieces).strip() + "\n"
    words = count_words(body)
    header = build_transcript_header(
        title=title, subject=subject, date_fr=date_fr, teacher=teacher, audio_name=audio_path.name,
        duration_sec=duration, segments=segments, overlap_sec=overlap_sec, model=model, words=words)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + body, encoding="utf-8")

    elapsed = time.time() - started
    log("Transcription écrite : {} ({} mots, run {})".format(out_path, words, format_time(elapsed)))
    return {"duration": duration, "chunks": total, "cached": cached_n, "words": words,
            "out_path": str(out_path), "elapsed": elapsed}
