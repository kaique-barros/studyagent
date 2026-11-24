"""Projeção do Kanban do Obsidian: o banco é a verdade; este arquivo é só uma visualização gerada."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from .config import Settings

FOOTER = '%% kanban:settings\n```\n{"kanban-plugin":"board"}\n```\n%%\n'


def _line(t: sqlite3.Row, done: bool) -> str:
    parts = [f"- [{'x' if done else ' '}] {t['title']}"]
    when = t["planned_for"] or t["due"]          # o Kanban/calendário mostram o dia de FAZER; o prazo continua no banco
    if when:
        parts.append(f"@{{{when}}}")
    if t["tag"]:
        parts.append(f"#{t['tag']}")
    return " ".join(parts)


def render(conn: sqlite3.Connection, settings: Settings, now: datetime) -> str:
    cutoff = (now - timedelta(days=settings.done_visible_days)).isoformat()
    open_rows = conn.execute(
        "SELECT * FROM task WHERE status='open' ORDER BY COALESCE(planned_for, due, '9999-12-31'), id").fetchall()
    done_rows = conn.execute(
        "SELECT * FROM task WHERE status='done' AND done_at >= ? ORDER BY done_at DESC, id DESC", (cutoff,)).fetchall()

    out = ["---", "", "kanban-plugin: board", "", "---", "",
           "<!-- Gerado pelo studyagent: edições manuais serão sobrescritas. -->", ""]
    out += [f"## {settings.lane_open}", ""]
    out += [_line(t, False) for t in open_rows]
    out += ["", f"## {settings.lane_done}", "", "**Complete**", ""]
    out += [_line(t, True) for t in done_rows]
    out += ["", "", ""]
    return "\n".join(out) + FOOTER


def write(conn: sqlite3.Connection, settings: Settings, now: datetime) -> bool:
    """Grava de forma atômica, só se o conteúdo mudou. Retorna True se escreveu."""
    path = settings.tasks_file
    if path is None:
        return False
    text = render(conn, settings, now)
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except (FileNotFoundError, PermissionError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".studyagent-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        os.chmod(path, 0o644)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True
