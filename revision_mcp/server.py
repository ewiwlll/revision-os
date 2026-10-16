"""MCP « revision » — pilote un pipeline de cours (audio → fiche + questions + cartes Anki).

Deux modes, choisis par l'environnement :

**Local** (aucun compte, aucun serveur — ta clé OpenRouter suffit) :
    REVISION_WORKSPACE=~/revision-os            # dossier de travail (créé au besoin)
    OPENROUTER_API_KEY=sk-or-...                # ta clé (transcription + génération)

**Hébergé** (une instance Revision SaaS, avec compte) :
    REVISION_SAAS_URL=https://...
    REVISION_SAAS_TOKEN=rsaas_...               # jeton créé dans /app/compte

Les deux peuvent être actifs en même temps (les outils des deux modes sont exposés).
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

try:  # SDK MCP v2 (MCPServer) — repli sur v1 (FastMCP)
    from mcp.server.mcpserver import MCPServer as _MCPServer
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP as _MCPServer

logging.getLogger("httpx").setLevel(logging.WARNING)  # stderr propre pour le client MCP

BASE = os.environ.get("REVISION_SAAS_URL", "").rstrip("/")
TOKEN = os.environ.get("REVISION_SAAS_TOKEN", "").strip()
WORKSPACE = Path(os.environ.get("REVISION_WORKSPACE", "")).expanduser() if os.environ.get("REVISION_WORKSPACE") else None

AUDIO_EXT = {".m4a", ".mp3", ".wav", ".ogg", ".aac", ".flac", ".aiff", ".mp4"}
ARTEFACTS = ("transcript.md", "plan.json", "fiche.md", "questions.json", "cards.json", "export.apkg")
FICHE_PREVIEW = 6000

mcp = _MCPServer("revision")


# =========================================================================== Local

def _slug(title: str) -> str:
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", folded).strip("-").lower()
    return slug[:60] or "cours"


def _course_dir(course: str) -> Path:
    if WORKSPACE is None:
        raise RuntimeError("Mode local non configuré : définir REVISION_WORKSPACE.")
    d = (WORKSPACE / "courses" / course).resolve()
    if not str(d).startswith(str(WORKSPACE.resolve()) + os.sep):
        raise RuntimeError("nom de cours invalide")
    if not d.is_dir():
        raise RuntimeError(f"cours « {course} » introuvable dans {WORKSPACE / 'courses'}")
    return d


def _course_status(d: Path) -> dict[str, Any]:
    out = d / "out"
    title = d.name
    plan = out / "plan.json"
    if plan.is_file():
        try:
            title = json.loads(plan.read_text(encoding="utf-8")).get("titre") or title
        except Exception:
            pass
    elif (out / "fiche.md").is_file():
        first = (out / "fiche.md").read_text(encoding="utf-8").splitlines()[:1]
        title = (first[0].lstrip("# ").strip() if first else title) or title
    present = [n for n in ARTEFACTS if (out / n).is_file()]
    src = [p.name for p in sorted((d / "src").glob("*")) if p.is_file()] if (d / "src").is_dir() else []
    if "fiche.md" in present:
        state = "prêt"
    elif "plan.json" in present:
        state = "plan prêt — génération à lancer ou en cours"
    elif "transcript.md" in present:
        state = "transcrit — plan à générer"
    elif src:
        state = "sources déposées — génération à lancer"
    else:
        state = "vide"
    log = d / "work" / "run.log"
    running = log.is_file() and (Path(d) / "out").stat().st_mtime >= log.stat().st_mtime - 1
    return {"id": d.name, "title": title, "state": state, "files": present, "src": src, "log": log}


@mcp.tool()
def liste_cours() -> str:
    """Liste les cours du dossier de travail local (id, titre, état)."""
    if WORKSPACE is None:
        raise RuntimeError("Mode local non configuré (REVISION_WORKSPACE).")
    root = WORKSPACE / "courses"
    if not root.is_dir():
        return f"Aucun cours — dossier vide ({root}). Crée-en un avec creer_cours."
    rows = [_course_status(p) for p in sorted(root.iterdir()) if p.is_dir()]
    if not rows:
        return f"Aucun cours dans {root}."
    return "\n".join(f"- {r['id']} · {r['title']} — {r['state']}" for r in rows)


@mcp.tool()
def creer_cours(titre: str) -> str:
    """Crée un cours local (dossier de travail) à partir d'un titre."""
    if WORKSPACE is None:
        raise RuntimeError("Mode local non configuré (REVISION_WORKSPACE).")
    d = WORKSPACE / "courses" / _slug(titre)
    for sub in ("src", "out", "work"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return f"cours créé : {d.name} — dépose l'audio avec deposer_fichier, puis lancer_generation."


@mcp.tool()
def deposer_fichier(cours: str, chemin: str) -> str:
    """Copie un fichier local (audio m4a/mp3/wav, PDF de diapos…) dans les sources du cours."""
    p = Path(chemin).expanduser()
    if not p.is_file():
        raise RuntimeError(f"fichier introuvable : {p}")
    d = _course_dir(cours)
    (d / "src").mkdir(exist_ok=True)
    dest = d / "src" / p.name
    if p.resolve() != dest.resolve():
        shutil.copy2(p, dest)
    return f"{p.name} ({p.stat().st_size // 1024} Ko) déposé dans {d / 'src'}"


@mcp.tool()
def lancer_generation(cours: str, sujet: str = "", date: str = "") -> str:
    """Lance la chaîne transcription → plan → fiche/questions/cartes en arrière-plan.

    Le journal est écrit dans <cours>/work/run.log ; suis l'avancement avec detail_cours.
    Compte ~1 min de traitement par 10 min d'audio (plus quelques minutes de LLM).
    """
    d = _course_dir(cours)
    sources = sorted((d / "src").glob("*")) if (d / "src").is_dir() else []
    audio = next((p for p in sources if p.suffix.lower() in AUDIO_EXT and p.is_file()), None)
    if audio is None:
        raise RuntimeError("aucun audio dans src/ — deposer_fichier d'abord")
    out = d / "out"
    out.mkdir(exist_ok=True)
    log = d / "work" / "run.log"
    log.parent.mkdir(exist_ok=True)
    cli = [sys.executable, "-m", "pipeline.cli"]
    cmds = [
        cli + ["ingest", "--audio", str(audio), "--out", str(out / "transcript.md"), "--title", sujet or d.name]
        + (["--date", date] if date else []),
        cli + ["plan", "--transcript", str(out / "transcript.md"), "--out", str(out / "plan.json"), "--title", sujet or d.name],
        cli + ["generate", "--transcript", str(out / "transcript.md"), "--plan", str(out / "plan.json"), "--outdir", str(out)],
    ]
    script = " && ".join(" ".join(shlex.quote(part) for part in cmd) for cmd in cmds)
    with log.open("ab") as fh:
        fh.write(f"\n== {audio.name} ==\n".encode())
        subprocess.Popen(["/bin/sh", "-c", script], stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    return f"génération lancée sur {audio.name} — journal : {log} (suivi : detail_cours)"


@mcp.tool()
def detail_cours(cours: str) -> str:
    """État d'un cours local : artefacts produits, sources, et fin du journal de génération."""
    d = _course_dir(cours)
    st = _course_status(d)
    lines = [f"{st['title']} — {st['state']} — {d}",
             "sources : " + (", ".join(st["src"]) or "aucune"),
             "produits : " + (", ".join(st["files"]) or "aucun")]
    log = st["log"]
    if log.is_file():
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
        lines.append("journal (fin) :\n" + "\n".join(tail))
    return "\n".join(lines)


@mcp.tool()
def fiche(cours: str, complet: bool = False) -> str:
    """Fiche markdown du cours local (aperçu 6000 caractères ; complet=True pour tout)."""
    path = _course_dir(cours) / "out" / "fiche.md"
    if not path.is_file():
        raise RuntimeError("pas encore de fiche — lancer_generation, puis patienter (detail_cours).")
    text = path.read_text(encoding="utf-8")
    if complet or len(text) <= FICHE_PREVIEW:
        return text
    return text[:FICHE_PREVIEW] + f"\n\n… [{len(text)} caractères au total — rappeler avec complet=True]"


@mcp.tool()
def cartes(cours: str, destination: str = "") -> str:
    """Cartes de révision du cours local. Écrit le JSON complet dans `destination` si fourni."""
    path = _course_dir(cours) / "out" / "cards.json"
    if not path.is_file():
        raise RuntimeError("pas encore de cartes — lancer_generation d'abord.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if destination:
        out = Path(destination).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = data.get("cards", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    preview = "\n".join(f"- {str(c.get('front', ''))[:80]}" for c in rows[:10])
    return f"{len(rows)} cartes" + (f" → écrit dans {destination}" if destination else "") + ("\n" + preview if preview else "")


@mcp.tool()
def exporter_anki(cours: str) -> str:
    """Construit le deck Anki (.apkg) à partir des cartes du cours local."""
    from pipeline.export import build_apkg  # même venv que le CLI

    d = _course_dir(cours)
    cards = d / "out" / "cards.json"
    if not cards.is_file():
        raise RuntimeError("pas encore de cartes — lancer_generation d'abord.")
    target = d / "out" / "export.apkg"
    build_apkg(cards_path=cards, out_path=target, deck_name=f"Revision::{_course_status(d)['title']}", course_id=d.name)
    return f"{target} ({target.stat().st_size // 1024} Ko) — à importer dans Anki"


@mcp.tool()
def telecharger_fichier(cours: str, nom: str, destination: str) -> str:
    """Copie un artefact local (fiche.md, questions.json, cards.json, transcript.md, plan.json, export.apkg) vers `destination`."""
    if nom not in ARTEFACTS:
        raise RuntimeError(f"nom attendu : {', '.join(ARTEFACTS)}")
    src = _course_dir(cours) / "out" / nom
    if not src.is_file():
        raise RuntimeError(f"{nom} absent — pas encore généré ?")
    dest = Path(destination).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return f"{nom} → {dest} ({dest.stat().st_size // 1024} Ko)"


# =========================================================================== API (instance hébergée)

def _client(timeout: float = 30.0) -> httpx.Client:
    if not BASE or not TOKEN:
        raise RuntimeError("Mode hébergé non configuré (REVISION_SAAS_URL + REVISION_SAAS_TOKEN).")
    return httpx.Client(base_url=BASE, timeout=timeout, headers={"Authorization": f"Bearer {TOKEN}"})


def _payload(r: httpx.Response) -> Any:
    try:
        data: Any = r.json()
    except Exception:
        data = {"raw": r.text[:400]}
    if r.status_code >= 400:
        detail = data.get("error") if isinstance(data, dict) else None
        raise RuntimeError(f"HTTP {r.status_code} — {detail or r.text[:200]}")
    return data


def _get(path: str, timeout: float = 30.0) -> Any:
    with _client(timeout) as c:
        return _payload(c.get(path))


def _post(path: str, payload: dict | None = None, timeout: float = 60.0) -> Any:
    with _client(timeout) as c:
        return _payload(c.post(path, json=payload if payload is not None else {}))


def _api_output_file(course_id: str, name: str) -> dict:
    for f in _get(f"/api/courses/{course_id}").get("files", []):
        if f.get("filename") == name or str(f.get("r2Key", "")).endswith("/" + name):
            return f
    raise RuntimeError(f"« {name} » introuvable pour ce cours (génération terminée ?)")


if TOKEN and BASE:  # les outils hébergés n'apparaissent que si une instance est configurée

    @mcp.tool()
    def api_qui_suis_je() -> str:
        """(Hébergé) Compte connecté, crédits, mode de facturation (crédits ou clé perso BYOK)."""
        d = _get("/api/me")
        u = d["user"]
        mode = "clé API perso (BYOK — pas de crédit consommé)" if d.get("byok") else f"{d['credits']} crédit(s)"
        return f"{u.get('name') or '(sans nom)'} <{u['email']}> — {mode} — {BASE}"

    @mcp.tool()
    def api_liste_cours() -> str:
        """(Hébergé) Liste les cours du compte (id, titre, statut, matière, chapitre, examen)."""
        rows = _get("/api/courses").get("courses", [])
        if not rows:
            return "Aucun cours."
        lines = []
        for c in rows:
            meta = [str(c.get("status"))]
            for key in ("subject", "chapter"):
                if c.get(key):
                    meta.append(str(c[key]))
            if c.get("examDate"):
                meta.append(f"examen {c['examDate']}")
            lines.append(f"- {c['id']} · {c.get('title')} — " + " · ".join(meta))
        return "\n".join(lines)

    @mcp.tool()
    def api_creer_cours(titre: str = "") -> str:
        """(Hébergé) Crée un cours (titre optionnel : rempli tout seul au dépôt de l'audio)."""
        c = _post("/api/courses", {"title": titre})["course"]
        return f"cours créé : {c['id']} — api_deposer_fichier puis api_lancer_generation."

    @mcp.tool()
    def api_deposer_fichier(course_id: str, chemin: str) -> str:
        """(Hébergé) Dépose un fichier local comme source du cours (audio, PDF…)."""
        p = Path(chemin).expanduser()
        if not p.is_file():
            raise RuntimeError(f"fichier introuvable : {p}")
        uid = _get("/api/me")["user"]["id"]
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        key = f"/api/files/u/{uid}/c/{course_id}/src/{urllib.parse.quote(p.name)}"
        with _client(1800) as c:
            with p.open("rb") as fh:
                _payload(c.put(key, content=fh, headers={"Content-Type": ctype}))
        return f"{p.name} déposé sur le cours {course_id}"

    @mcp.tool()
    def api_lancer_generation(course_id: str) -> str:
        """(Hébergé) Lance transcription → plan → génération (la chaîne s'enchaîne seule côté serveur)."""
        j = _post(f"/api/courses/{course_id}/ingest").get("job", {})
        return f"job {j.get('id')} lancé — suivi : api_statut_job"

    @mcp.tool()
    def api_statut_job(job_id: str) -> str:
        """(Hébergé) État d'un job de génération (status, étape, progression, erreur)."""
        j = _get(f"/api/jobs/{job_id}")["job"]
        return f"{j.get('type')} · {j.get('status')} · {j.get('stage') or ''} · {j.get('progress')}%" + (
            f" · erreur : {j.get('error')}" if j.get("error") else "")

    @mcp.tool()
    def api_fiche(course_id: str, complet: bool = False) -> str:
        """(Hébergé) Fiche markdown du cours (aperçu 6000 caractères)."""
        row = _api_output_file(course_id, "fiche.md")
        with _client(60) as c:
            r = c.get(f"/api/files/{row['r2Key']}")
            r.raise_for_status()
            text = r.text
        return text if complet or len(text) <= FICHE_PREVIEW else text[:FICHE_PREVIEW] + f"\n\n… [{len(text)} caractères au total]"

    @mcp.tool()
    def api_cartes(course_id: str, destination: str = "") -> str:
        """(Hébergé) Cartes de révision ; écrit le JSON complet dans `destination` si fourni."""
        row = _api_output_file(course_id, "cards.json")
        data = _get(f"/api/files/{row['r2Key']}", timeout=60.0)
        if destination:
            out = Path(destination).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        rows = data.get("cards", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        preview = "\n".join(f"- {str(c.get('front', ''))[:80]}" for c in rows[:10])
        return f"{len(rows)} cartes" + (f" → {destination}" if destination else "") + ("\n" + preview if preview else "")

    @mcp.tool()
    def api_telecharger_fichier(course_id: str, nom: str, destination: str) -> str:
        """(Hébergé) Télécharge un artefact du cours (fiche.md, questions.json, cards.json, transcript.md, plan.json, export.apkg)."""
        if nom not in ARTEFACTS:
            raise RuntimeError(f"nom attendu : {', '.join(ARTEFACTS)}")
        row = _api_output_file(course_id, nom)
        out = Path(destination).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        with _client(300) as c:
            with c.stream("GET", f"/api/files/{row['r2Key']}") as r:
                r.raise_for_status()
                with out.open("wb") as fh:
                    for chunk in r.iter_bytes():
                        fh.write(chunk)
        return f"{nom} → {out} ({out.stat().st_size // 1024} Ko)"


def main() -> None:
    if WORKSPACE is None and not (TOKEN and BASE):
        raise SystemExit(
            "Configure au moins un mode :\n"
            "  local   → REVISION_WORKSPACE=~/revision-os (+ OPENROUTER_API_KEY pour générer)\n"
            "  hébergé → REVISION_SAAS_URL + REVISION_SAAS_TOKEN"
        )
    mcp.run()


if __name__ == "__main__":
    main()
