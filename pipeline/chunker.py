"""Découpage audio — porté de BioMIA/scripts/ingest_course.py."""
from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

from .errors import IngestionError
from .text import format_time

DEFAULT_CHUNK_MINUTES = 17.0
CHUNK_MIN_MINUTES = 9.0
CHUNK_MAX_MINUTES = 18.0
DEFAULT_OVERLAP_SEC = 3.0


def get_audio_duration(audio_path) -> float:
    """Durée en secondes via ffprobe (lecture seule)."""
    if shutil.which("ffprobe") is None:
        raise IngestionError("ffprobe introuvable (installer ffmpeg).")
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    raw = (res.stdout or "").strip().splitlines()
    if res.returncode != 0 or not raw:
        raise IngestionError(
            "ffprobe n'a pas pu lire « {} » : {}".format(audio_path, (res.stderr or "").strip()[:200])
        )
    try:
        duration = float(raw[0])
    except ValueError:
        raise IngestionError(
            "Durée illisible pour « {} » (sortie ffprobe : {!r}).".format(audio_path, raw[0])
        )
    if duration <= 0:
        raise IngestionError("Durée nulle ou négative pour « {} ».".format(audio_path))
    return duration


def compute_segments(total_sec: float, chunk_minutes: float = DEFAULT_CHUNK_MINUTES,
                     min_minutes: float = CHUNK_MIN_MINUTES, max_minutes: float = CHUNK_MAX_MINUTES,
                     overlap_sec: float = DEFAULT_OVERLAP_SEC) -> list[tuple[int, float, float]]:
    """Découpe en N chunks dont la durée reste dans [min_minutes, max_minutes].

    N est calé sur la cible chunk_minutes puis ajusté pour respecter les bornes
    (un enregistrement plus court que la borne min tient en 1 seul chunk).
    Les chunks 2..N démarrent `overlap_sec` avant leur borne théorique : aucun mot
    prononcé à cheval sur une frontière n'est perdu.

    Retourne une liste de tuples (part_index, start_sec, end_sec).
    """
    if total_sec <= 0:
        raise IngestionError("Durée audio invalide ({} s).".format(total_sec))
    target_sec = max(1.0, float(chunk_minutes) * 60.0)
    min_sec = max(1.0, float(min_minutes) * 60.0)
    max_sec = max(min_sec, float(max_minutes) * 60.0)

    count = max(1, int(math.ceil(total_sec / target_sec)))
    while count > 1 and (total_sec / count) < min_sec:
        count -= 1
    while (total_sec / count) > max_sec:
        count += 1

    step = total_sec / float(count)
    segments = []
    for i in range(count):
        start = 0.0 if i == 0 else max(0.0, i * step - float(overlap_sec))
        end = total_sec if i == count - 1 else min(total_sec, (i + 1) * step)
        segments.append((i + 1, start, end))
    return segments


def extract_and_filter_chunk(input_path, start_sec: float, end_sec: float, output_path) -> None:
    """Extrait le créneau et applique le filtre acoustique du pipeline
    (highpass + loudnorm, 16 kHz mono, AAC 64 kbit/s)."""
    if shutil.which("ffmpeg") is None:
        raise IngestionError("ffmpeg introuvable (installer ffmpeg).")
    cmd = [
        "ffmpeg", "-y",
        "-ss", "{:.3f}".format(start_sec),
        "-t", "{:.3f}".format(max(0.0, end_sec - start_sec)),
        "-i", str(input_path),
        "-af", "highpass=f=80,loudnorm",
        "-ar", "16000",
        "-ac", "1",
        "-c:a", "aac",
        "-b:a", "64k",
        str(output_path),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0 or not Path(output_path).exists():
        raise IngestionError(
            "FFmpeg a échoué sur le créneau {} -> {} : {}".format(
                format_time(start_sec), format_time(end_sec), (res.stderr or "").strip()[-300:]
            )
        )
