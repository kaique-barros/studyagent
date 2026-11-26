"""Vigia a pasta de cronogramas e envia propostas `schedule.import` ao daemon (token `schedule`).

Estrutura esperada: <raiz>/<AAAA-S>/Matérias/<Sigla>/Cronograma.pdf  (ex.: .../UFMG/2026-2/Matérias/DCC216/Cronograma.pdf).
Só reprocessa quando o hash do PDF muda; nada é aplicado sem sua aprovação.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .schedule import find_pdfs, parse_pdf, sha256_file


def propose(client: httpx.Client, path: Path, semester: str, key: str, llm: Any | None = None, reason: str | None = None) -> dict[str, Any]:
    h = sha256_file(path)
    course = parse_pdf(path, semester, key, llm)
    body = {"kind": "schedule.import", "schedule": course.payload(h), "reason": reason or f"cronograma de {key} ({semester})"}
    r = client.post("/proposals", json=body)
    if r.status_code != 201:
        raise RuntimeError(f"o daemon recusou o cronograma de {key}: {r.status_code} {r.text[:200]}")
    kinds: dict[str, int] = {}
    for s in course.sessions:
        kinds[s.kind] = kinds.get(s.kind, 0) + 1
    return {"proposal": r.json()["id"], "key": key, "semester": semester, "method": course.method,
            "sessions": len(course.sessions), "kinds": kinds, "warnings": course.warnings}


def known_hashes(client: httpx.Client) -> set[str]:
    """Hashes já importados ou já propostos (pendentes): não precisam ser reprocessados."""
    r = client.get("/courses")
    if r.status_code == 404:
        raise RuntimeError("o daemon em execução é uma versão antiga (sem /courses): reinicie o `studyagent serve`")
    if r.status_code >= 400:
        raise RuntimeError(f"o daemon recusou a consulta ({r.status_code}): {r.text[:200]}")
    out = {c["source_hash"] for c in r.json() if c.get("source_hash")}
    r = client.get("/proposals", params={"status": "pending"})
    if r.status_code >= 400:
        raise RuntimeError(f"o daemon recusou a consulta ({r.status_code}): {r.json().get('detail', r.text)}. "
                           "Se o token foi criado antes desta versão, reinicie o `studyagent serve`")
    for p in r.json():
        if p["kind"] == "schedule.import":
            out.add(p["payload"]["source_hash"])
    return out


def scan(client: httpx.Client, root: Path, llm: Any | None = None, failed: set[str] | None = None) -> list[dict[str, Any]]:
    failed = failed if failed is not None else set()
    done = known_hashes(client)
    results = []
    for semester, key, pdf in find_pdfs(root):
        h = sha256_file(pdf)
        if h in done or h in failed:
            continue
        try:
            results.append(propose(client, pdf, semester, key, llm, reason=f"cronograma de {key} ({semester}) novo ou alterado"))
        except Exception as e:  # um PDF ruim não pode impedir os outros
            failed.add(h)
            results.append({"key": key, "semester": semester, "error": str(e)})
    return results
