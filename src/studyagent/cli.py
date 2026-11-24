from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
import typer

from . import auth
from .config import Settings
from .db import connect, migrate

app = typer.Typer(help="studyagent: cliente da API local e administração do daemon", no_args_is_help=True)
task_app = typer.Typer(help="tarefas", no_args_is_help=True)
prop_app = typer.Typer(help="propostas pendentes de confirmação", no_args_is_help=True)
admin_app = typer.Typer(help="administração (rode como o usuário do agente)", no_args_is_help=True)
dayoff_app = typer.Typer(help="dias de folga (meta menor)", no_args_is_help=True)
plan_app = typer.Typer(help="planejador", no_args_is_help=True)
app.add_typer(dayoff_app, name="dayoff")
app.add_typer(plan_app, name="plan")
app.add_typer(task_app, name="task")
app.add_typer(prop_app, name="proposal")
app.add_typer(admin_app, name="admin")


# ---------- cliente ----------
def _token() -> str:
    t = os.environ.get("STUDYAGENT_TOKEN")
    if t:
        return t.strip()
    f = Path(os.environ.get("STUDYAGENT_TOKEN_FILE") or Path.home() / ".config" / "studyagent" / "token")
    if f.exists():
        return f.read_text().strip()
    typer.echo("token não encontrado (STUDYAGENT_TOKEN ou ~/.config/studyagent/token)", err=True)
    raise typer.Exit(2)


def _client() -> httpx.Client:
    sock = os.environ.get("STUDYAGENT_SOCKET") or str(Settings().socket)
    return httpx.Client(transport=httpx.HTTPTransport(uds=sock), base_url="http://studyagent",
                        headers={"Authorization": f"Bearer {_token()}"}, timeout=15)


def _call(method: str, path: str, **kw):
    try:
        with _client() as c:
            r = c.request(method, path, **kw)
    except httpx.TransportError as e:
        typer.echo(f"não consegui falar com o daemon: {e}", err=True)
        raise typer.Exit(3)
    if r.status_code >= 400:
        typer.echo(f"erro {r.status_code}: {r.json().get('detail', r.text) if r.headers.get('content-type','').startswith('application/json') else r.text}", err=True)
        raise typer.Exit(1)
    return r.json()


def _show_task(t: dict) -> str:
    when = t.get("planned_for") or t.get("due") or "-"
    mark = {"open": " ", "done": "x", "dropped": "-"}.get(t["status"], "?")
    return f"[{mark}] #{t['id']:<4} {when:<10} {t['title']}" + (f"  #{t['tag']}" if t.get("tag") else "")


@app.command()
def status():
    """Resumo do dia."""
    typer.echo(json.dumps(_call("GET", "/summary"), indent=2, ensure_ascii=False))


@task_app.command("list")
def task_list(status: Optional[str] = typer.Option("open", help="open | done | dropped | (vazio = todas)"),
              json_out: bool = typer.Option(False, "--json")):
    data = _call("GET", "/tasks", params={"status": status} if status else {})
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False) if json_out else "\n".join(_show_task(t) for t in data) or "(nenhuma)")


@task_app.command("add")
def task_add(title: str, due: Optional[str] = typer.Option(None, help="aaaa-mm-dd"), tag: Optional[str] = None,
             course: Optional[str] = None, minutes: Optional[int] = typer.Option(None, "--minutes")):
    body = {"title": title, "due": due, "tag": tag, "course": course, "est_minutes": minutes}
    typer.echo(_show_task(_call("POST", "/tasks", json={k: v for k, v in body.items() if v is not None})))


@task_app.command("done")
def task_done(task_id: int):
    typer.echo(_show_task(_call("POST", f"/tasks/{task_id}/complete")))


@task_app.command("move")
def task_move(task_id: int, planned_for: Optional[str] = typer.Option(None, "--plan"), due: Optional[str] = None,
              reason: Optional[str] = None):
    """Pede para mudar data (vira proposta; confirme com `studyagent proposal approve`)."""
    body = {k: v for k, v in {"planned_for": planned_for, "due": due, "reason": reason}.items() if v}
    typer.echo(json.dumps(_call("POST", f"/tasks/{task_id}/reschedule", json=body), ensure_ascii=False))


@task_app.command("drop")
def task_drop(task_id: int, reason: Optional[str] = None):
    """Pede para descartar uma tarefa (vira proposta)."""
    typer.echo(json.dumps(_call("POST", f"/tasks/{task_id}/drop", json={"reason": reason}), ensure_ascii=False))


@prop_app.command("list")
def prop_list(status: str = "pending"):
    for p in _call("GET", "/proposals", params={"status": status}):
        pl = p["payload"]
        if p["kind"] == "plan.apply":
            pl = f"{len(pl['moves'])} movimentos (veja `studyagent plan show`)"
        elif p["kind"] == "dayoff.add":
            pl = "folgas: " + ", ".join(pl["dates"])
        typer.echo(f"#{p['id']:<4} {p['kind']:<16} tarefa={p['task_id']} origem={p['source']} {pl}  — {p.get('reason') or ''}")


@prop_app.command("approve")
def prop_approve(pid: int):
    typer.echo(json.dumps(_call("POST", f"/proposals/{pid}/approve"), ensure_ascii=False))


@prop_app.command("reject")
def prop_reject(pid: int):
    typer.echo(json.dumps(_call("POST", f"/proposals/{pid}/reject"), ensure_ascii=False))


@dayoff_app.command("list")
def dayoff_list():
    typer.echo("\n".join(_call("GET", "/dayoffs")) or "(nenhuma)")


@dayoff_app.command("add")
def dayoff_add(day: str = typer.Argument(..., help="aaaa-mm-dd (a partir de amanhã)")):
    r = _call("POST", "/dayoffs", json={"date": day})
    typer.echo(f"folga em {r['date']}" + ("" if r["added"] else " (já existia)"))


@dayoff_app.command("remove")
def dayoff_remove(day: str):
    _call("DELETE", f"/dayoffs/{day}")
    typer.echo(f"folga de {day} removida")


@plan_app.command("show")
def plan_show():
    """Mostra o plano que o planejador faria agora (não grava nada)."""
    p = _call("GET", "/planner/preview")
    typer.echo(f"programadas: {p['programmed']}/{p['programmed_goal']}" + ("" if p["programmed_ok"] else "  (abaixo da meta!)"))
    for m in p["moves"]:
        typer.echo(f"  #{m['task_id']:<4} {m['from'] or '-':<10} -> {m['to'] or '-':<10} {m['title']}")
    if not p["moves"]:
        typer.echo("  nenhum movimento: o cronograma já está de acordo")
    for label, key in (("sobrecarga", "overloaded"), ("faltam tarefas", "shortfall")):
        if p[key]:
            typer.echo(f"{label}: " + ", ".join(f"{d} ({n})" for d, n in p[key].items()))
    if p["overdue"]:
        typer.echo(f"atrasadas: {p['overdue']}")
    if p["dayoff_suggestions"]:
        typer.echo("folgas sugeridas: " + ", ".join(p["dayoff_suggestions"]))


@plan_app.command("run")
def plan_run():
    """Gera as propostas do plano agora (aprove com `studyagent proposal approve`)."""
    r = _call("POST", "/planner/run")
    typer.echo(f"{r['moves']} movimentos; propostas criadas: {r['created_proposals'] or 'nenhuma (nada novo)'}")


@app.command()
def events(after: int = 0, follow: bool = typer.Option(False, "-f", help="acompanha em tempo real (SSE)")):
    if not follow:
        for e in _call("GET", "/events", params={"after": after}):
            typer.echo(json.dumps(e, ensure_ascii=False))
        return
    with _client() as c, c.stream("GET", "/events/stream", params={"after": after}, timeout=None) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                typer.echo(line[6:])


# ---------- administração (acesso direto ao banco) ----------
@admin_app.command("init")
def admin_init():
    """Cria o diretório de estado e aplica as migrações."""
    s = Settings()
    s.state_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(s.db_path)
    migrate(conn)
    typer.echo(f"banco pronto em {s.db_path}")


@admin_app.command("token-create")
def admin_token_create(name: str, scope: str,
                       out: Optional[Path] = typer.Option(None, help="grava o token neste arquivo (0600)"),
                       replace: bool = typer.Option(False, "--replace", help="substitui um token existente com o mesmo nome")):
    """Gera um token. Escopos: user, moodle, planner, schedule, viewer. O token só aparece uma vez."""
    s = Settings()
    conn = connect(s.db_path)
    migrate(conn)
    if replace:
        conn.execute("DELETE FROM token WHERE name=?", (name,))
    try:
        token = auth.create_token(conn, name, scope, datetime.now().astimezone().replace(microsecond=0).isoformat())
    except ValueError as e:
        typer.echo(str(e), err=True); raise typer.Exit(1)
    except sqlite3.IntegrityError:
        typer.echo(f"já existe um token chamado '{name}'. Use --replace para gerar outro no lugar dele "
                   "(o antigo para de funcionar) ou escolha outro nome.", err=True)
        raise typer.Exit(1)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        typer.echo(f"token '{name}' ({scope}) gravado em {out}")
    else:
        typer.echo(token)


@admin_app.command("token-revoke")
def admin_token_revoke(name: str):
    conn = connect(Settings().db_path)
    typer.echo("revogado" if auth.revoke_token(conn, name) else "não encontrado")


@admin_app.command("rebuild")
def admin_rebuild():
    """Regera o Tarefas.md a partir do banco."""
    from . import projection
    from .service import system_clock
    s = Settings()
    conn = connect(s.db_path)
    typer.echo("escrito" if projection.write(conn, s, system_clock()) else "sem mudanças (ou STUDYAGENT_TASKS_FILE não definido)")


@app.command()
def serve():
    """Sobe o daemon (socket Unix)."""
    from .daemon import serve as _serve
    _serve()


if __name__ == "__main__":
    app()
