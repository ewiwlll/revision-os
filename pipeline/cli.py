"""CLI du worker — smoke tests et outils. Usage : python3 -m pipeline.cli <commande>."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from . import __version__
from .chunker import compute_segments, extract_and_filter_chunk, get_audio_duration
from .config import Config
from .errors import IngestionError, QuotaAbort
from .export import build_apkg
from .generate import run_generate
from .ingest import run_ingest
from .plan import build_plan
from .prompt import build_transcription_prompt
from .text import ensure_leading_timestamp, realign_timestamps, strip_overlap_prefix
from .transcribe import transcribe_chunk
from .usage import UsageLedger, prices_for, usage_entry


def _ok(label: str, cond: bool, failures: list[str]) -> None:
    print(("  [ok] " if cond else "  [KO] ") + label)
    if not cond:
        failures.append(label)


def cmd_selftest(args: argparse.Namespace) -> int:
    failures: list[str] = []

    # 1) Découpage
    segs = compute_segments(600.0)
    _ok("600 s → 1 chunk", len(segs) == 1 and segs[0][1] == 0.0 and segs[0][2] == 600.0, failures)
    segs = compute_segments(3600.0)
    durations = [e - s for _, s, e in segs]
    _ok("3600 s → 4 chunks", len(segs) == 4, failures)
    _ok("durées dans [900, 903] s (overlap compris)", all(900.0 <= d <= 903.0 for d in durations), failures)
    _ok("chunk 2 démarre à 897 s (overlap 3 s)", abs(segs[1][1] - 897.0) < 0.5, failures)
    _ok("bornes 9-18 min respectées", all(540.0 <= d <= 1080.0 for d in durations), failures)
    _ok("4500 s → 5 chunks", len(compute_segments(4500.0)) == 5, failures)

    # 2) Dédup de chevauchement
    prev = "… l'enveloppe cellulaire est une membrane semi-perméable qui contrôle les échanges"
    nxt = ("l'enveloppe cellulaire est une membrane semi-perméable qui contrôle les échanges "
           "entre la cellule et son milieu.")
    cleaned, removed = strip_overlap_prefix(prev, nxt)
    _ok("chevauchement retiré", removed >= 5 and cleaned.startswith("entre la cellule"), failures)
    cleaned2, removed2 = strip_overlap_prefix("rien de commun ici", "autre chose totalement")
    _ok("pas de faux positif de dédup", removed2 == 0 and cleaned2 == "autre chose totalement", failures)

    # 3) Timecodes
    shifted, changed = realign_timestamps("[00:05] bonjour.\n[00:40] suite.", 1200.0)
    _ok("réalignement [00:05] → [20:05]", changed and "[20:05]" in shifted, failures)
    lead = ensure_leading_timestamp("bonjour", 120.0)
    _ok("timestamp de tête ajouté", lead.startswith("[02:00] "), failures)

    # 4) Ledger + prix
    prices = prices_for("google/gemini-3.8-flash")
    _ok("prices_for préfixe OpenRouter", prices == {"text": 0.75, "audio": 0.75, "output": 3.75}, failures)
    _ok("prices_for deepseek", prices_for("deepseek/deepseek-v4.1-flash")["output"] == 0.60, failures)
    with tempfile.TemporaryDirectory() as td:
        ledger = UsageLedger(Path(td) / "usage.jsonl")
        entry = usage_entry(provider="openrouter", model="google/gemini-3.8-flash", chunk="part1",
                            start_sec=0.0, end_sec=60.0,
                            usage={"prompt_tokens": 2000, "completion_tokens": 500}, prices=prices)
        ledger.append(entry)
        _ok("ledger append + spent_since", ledger.spent_since("0") > 0, failures)

    # 5) Audio (ffmpeg) si disponible
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "sine.wav"
            out = Path(td) / "part1.m4a"
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                 "-ar", "16000", "-ac", "1", str(src)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            extract_and_filter_chunk(src, 0.0, 2.0, out)
            dur = get_audio_duration(out)
            _ok("extraction ffmpeg + durée ≈ 2 s", abs(dur - 2.0) < 0.4, failures)
    else:
        print("  [sk] ffmpeg/ffprobe absents — partie audio sautée")

    if failures:
        print("SELFTEST KO : {} échec(s) : {}".format(len(failures), failures))
        return 1
    print("SELFTEST OK — worker prêt.")
    return 0


def cmd_chunk_plan(args: argparse.Namespace) -> int:
    segs = compute_segments(args.seconds, args.chunk_minutes, args.chunk_min, args.chunk_max,
                            args.overlap)
    print(json.dumps(
        [{"part": i, "start": round(s, 1), "end": round(e, 1)} for i, s, e in segs], indent=2))
    return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    cfg = Config.from_env()
    if args.model:
        cfg = replace(cfg, transcribe_model=args.model)
    if not cfg.openrouter_api_key:
        raise IngestionError("OPENROUTER_API_KEY absent de l'environnement.")
    src = Path(args.audio)
    if not src.exists():
        raise IngestionError("Audio introuvable : {}".format(src))

    total = get_audio_duration(src)
    start = float(args.start)
    end = min(total, start + float(args.seconds))
    with tempfile.TemporaryDirectory() as td:
        chunk = Path(td) / "part1.m4a"
        extract_and_filter_chunk(src, start, end, chunk)
        prompt = build_transcription_prompt(
            label="part1", start_sec=start, end_sec=end, total_sec=total,
            title=args.title, subject=args.subject, date_fr=args.date, teacher=args.teacher)
        print("→ {} → chunk {:.1f}s via {}".format(src.name, end - start, cfg.transcribe_model))
        text, usage = transcribe_chunk(cfg.openrouter_api_key, cfg.transcribe_model, chunk, prompt,
                                       effort=cfg.reasoning_effort)

    text = ensure_leading_timestamp(text, start)
    text, _ = realign_timestamps(text, start)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print("→ écrit : {}".format(out))
    else:
        print("----")
        print(text)
        print("----")

    entry = usage_entry(provider="openrouter", model=cfg.transcribe_model, chunk="part1",
                        start_sec=start, end_sec=end, usage=usage,
                        prices=prices_for(cfg.transcribe_model))
    UsageLedger(cfg.work_dir / "usage.jsonl").append(entry)
    print("💰 coût ≈ ${:.4f} ({} tok in / {} tok out)".format(
        entry["costUSD"], entry["promptTokens"], entry["outputTokens"]))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = Config.from_env()
    if args.model:
        cfg = replace(cfg, transcribe_model=args.model)
    if not cfg.openrouter_api_key:
        raise IngestionError("OPENROUTER_API_KEY absent de l'environnement.")
    ledger = UsageLedger(cfg.work_dir / "usage.jsonl")

    def progress(done: int, total: int, detail: str) -> None:
        print("⏳ {}/{} — {}".format(done, total, detail), flush=True)

    result = run_ingest(
        audio_path=args.audio, work_dir=cfg.work_dir / "ingest", out_path=args.out,
        model=cfg.transcribe_model, api_key=cfg.openrouter_api_key, effort=cfg.reasoning_effort,
        concurrency=args.concurrency, chunk_minutes=args.chunk_minutes, chunk_min=args.chunk_min,
        chunk_max=args.chunk_max, overlap_sec=args.overlap, title=args.title, subject=args.subject,
        date_fr=args.date, teacher=args.teacher, ledger=ledger, progress=progress)
    print("✅ {} — {} chunk(s) ({} en cache), {} mots, run {:0.1f} s".format(
        result["out_path"], result["chunks"], result["cached"], result["words"], result["elapsed"]))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    cfg = Config.from_env()
    if args.model:
        cfg = replace(cfg, gen_model=args.model)
    if not cfg.openrouter_api_key:
        raise IngestionError("OPENROUTER_API_KEY absent de l'environnement.")
    transcript = Path(args.transcript).read_text(encoding="utf-8")
    ledger = UsageLedger(cfg.work_dir / "usage.jsonl")
    plan = build_plan(transcript_md=transcript, api_key=cfg.openrouter_api_key,
                      model=cfg.gen_model, title=args.title, subject=args.subject,
                      effort=cfg.reasoning_effort, ledger=ledger)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print("✅ plan → {} ({} section(s))".format(out, len(plan["sections"])))
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    cfg = Config.from_env()
    if args.model:
        cfg = replace(cfg, gen_model=args.model)
    if not cfg.openrouter_api_key:
        raise IngestionError("OPENROUTER_API_KEY absent de l'environnement.")
    transcript = Path(args.transcript).read_text(encoding="utf-8")
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    images = None
    if args.images:
        images = json.loads(Path(args.images).read_text(encoding="utf-8"))
    ledger = UsageLedger(cfg.work_dir / "usage.jsonl")
    result = run_generate(transcript_md=transcript, plan=plan, out_dir=args.outdir,
                          api_key=cfg.openrouter_api_key, model=cfg.gen_model,
                          effort=cfg.reasoning_effort, ledger=ledger, images=images)
    print("✅ {} — fiche {} mots, {} question(s) → {} carte(s) (1 carte par question)".format(
        result["out_dir"], result["fiche_words"], result["questions"], result["cards"]))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    cards = Path(args.cards)
    if not cards.is_file():
        raise IngestionError("cards.json introuvable : {}".format(cards))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    result = build_apkg(cards_path=cards, out_path=out,
                        deck_name=args.deck or "Revision", course_id=args.course or "local")
    print("✅ deck Anki → {} ({} carte(s))".format(out, result.get("cards", result)))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="revision-pipeline", description="revision-os v" + __version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("selftest", help="vérifie modules + binaires (aucun réseau)")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("chunk-plan", help="affiche le découpage pour une durée")
    p.add_argument("--seconds", type=float, required=True)
    p.add_argument("--chunk-minutes", type=float, default=17.0)
    p.add_argument("--chunk-min", type=float, default=9.0)
    p.add_argument("--chunk-max", type=float, default=18.0)
    p.add_argument("--overlap", type=float, default=3.0)
    p.set_defaults(func=cmd_chunk_plan)

    p = sub.add_parser("transcribe", help="transcrit un extrait audio (OpenRouter, coût réel)")
    p.add_argument("--audio", required=True)
    p.add_argument("--seconds", type=float, default=45.0)
    p.add_argument("--start", type=float, default=0.0)
    p.add_argument("--out")
    p.add_argument("--title", default="Extrait de test")
    p.add_argument("--subject")
    p.add_argument("--date", dest="date")
    p.add_argument("--teacher")
    p.add_argument("--model")
    p.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("ingest", help="ingère un audio complet (chunks + cache + assemblage)")
    p.add_argument("--audio", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--title", default="Cours")
    p.add_argument("--subject")
    p.add_argument("--date", dest="date")
    p.add_argument("--teacher")
    p.add_argument("--model")
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--chunk-minutes", type=float, default=17.0)
    p.add_argument("--chunk-min", type=float, default=9.0)
    p.add_argument("--chunk-max", type=float, default=18.0)
    p.add_argument("--overlap", type=float, default=3.0)
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("plan", help="transcript → plan JSON structuré (étape éditable)")
    p.add_argument("--transcript", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--title")
    p.add_argument("--subject")
    p.add_argument("--model")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("export", help="cards.json → deck Anki .apkg")
    p.add_argument("--cards", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--deck", help="nom du deck Anki")
    p.add_argument("--course", help="identifiant du cours (métadonnée)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("generate", help="plan + transcript → fiche + questions + cartes (audits v0 en gates)")
    p.add_argument("--transcript", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--outdir", required=True)
    p.add_argument("--model")
    p.add_argument("--images", help="manifest.json des images de diaporama (illustrations optionnelles)")
    p.set_defaults(func=cmd_generate)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except QuotaAbort as exc:
        print("QUOTA : {}".format(exc), file=sys.stderr)
        return 2
    except IngestionError as exc:
        print("ERREUR : {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
