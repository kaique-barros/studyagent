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
agent_app = typer.Typer(help="agente com LLM (Groq): avalia o cronograma e dá feedback; usa o token `planner`", no_args_is_help=True)
schedule_app = typer.Typer(help="cronogramas de aula (PDF) -> matérias, calendário e tarefas de estudo", no_args_is_help=True)
app.add_typer(agent_app, name="agent")
app.add_typer(schedule_app, name="schedule")
app.add_typer(task_app, name="task")
ui_app = typer.Typer(help="saída JSON para o shell (Quickshell)", no_args_is_help=True)
app.add_typer(ui_app, name="ui")
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


@task_app.command("start")
def task_start(task_id: int):
    """Inicia o cronômetro da tarefa (pausa a que estiver rodando)."""
    typer.echo(_show_task(_call("POST", f"/tasks/{task_id}/start")))


@task_app.command("pause")
def task_pause(task_id: int):
    typer.echo(_show_task(_call("POST", f"/tasks/{task_id}/pause")))


@ui_app.command("snapshot")
def ui_snapshot(before: int = typer.Option(31, help="dias antes de hoje no calendário"),
                after: int = typer.Option(45, help="dias depois de hoje")):
    """Tudo que a tela de produtividade precisa, em um JSON compacto."""
    typer.echo(json.dumps(_call("GET", "/snapshot", params={"before": before, "after": after}), ensure_ascii=False, separators=(",", ":")))


@ui_app.command("progress")
def ui_progress():
    typer.echo(json.dumps(_call("GET", "/progress"), ensure_ascii=False, separators=(",", ":")))


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
        elif p["kind"] == "schedule.import":
            kinds: dict[str, int] = {}
            for x in pl["sessions"]:
                kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
            pl = f"{pl['key']} {pl['semester']}: {kinds}" + (f" ⚠ {len(pl['warnings'])} aviso(s)" if pl["warnings"] else "")
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


def _agent_state_dir() -> Path:
    sd = os.environ.get("STATE_DIRECTORY")
    return Path(sd.split(":")[0]) if sd else Path.home() / ".local" / "state" / "studyagent-agent"


def _agent_llm():
    from .llm import LLMError, from_env
    try:
        return from_env(_agent_state_dir())
    except LLMError as e:
        typer.echo(str(e), err=True); raise typer.Exit(2)


@agent_app.command("evaluate")
def agent_evaluate(force: bool = typer.Option(False, "--force", help="roda mesmo que já tenha rodado hoje")):
    """Avalia o cronograma e cria propostas (você aprova). Roda no máximo 1x por dia, salvo --force."""
    from .agent import Api, evaluate
    from .llm import LLMError
    stamp = _agent_state_dir() / "last-evaluate"
    with _client() as c:
        api = Api(c)
        today = api.get("/summary")["today"]
        if not force and stamp.exists() and stamp.read_text().strip() == today:
            typer.echo("já avaliado hoje (use --force)"); return
        try:
            out = evaluate(api, _agent_llm())
        except LLMError as e:
            typer.echo(f"erro do LLM: {e}", err=True); raise typer.Exit(1)
    if "skipped" not in out:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(today)
    typer.echo(json.dumps(out, indent=2, ensure_ascii=False))


@agent_app.command("review")
def agent_review(days: int = typer.Option(7, help="janela da revisão (7; pode ser 3-4)")):
    """Feedback da semana (números do próprio daemon + texto do LLM; sem LLM, só os números)."""
    from .agent import Api, review
    from .llm import LLMError, from_env
    try:
        llm = from_env(_agent_state_dir())
    except LLMError:
        llm = None
    with _client() as c:
        out = review(Api(c), llm, days)
    typer.echo(out["text"])


def _schedule_llm():
    from .llm import LLMError, from_env
    try:
        return from_env(_agent_state_dir())
    except LLMError:
        return None                                   # sem chave: só o leitor de tabelas


def _print_result(r: dict) -> None:
    if "error" in r:
        typer.echo(f"✗ {r['semester']}/{r['key']}: {r['error']}", err=True); return
    typer.echo(f"✓ {r['semester']}/{r['key']}: proposta #{r['proposal']} ({r['sessions']} linhas {r['kinds']}, via {r['method']})")
    for w in r["warnings"]:
        typer.echo(f"    ⚠ {w}")


@schedule_app.command("import")
def schedule_import(pdf: Path, semester: Optional[str] = typer.Option(None, help="AAAA-S; padrão: vem do caminho"),
                    key: Optional[str] = typer.Option(None, help="sigla; padrão: vem do caminho")):
    """Lê um Cronograma.pdf e cria a proposta de importação (você aprova depois)."""
    from .schedule import semester_key_from_path
    from .schedule_scan import propose
    guess = semester_key_from_path(pdf)
    semester, key = semester or (guess[0] if guess else None), key or (guess[1] if guess else None)
    if not semester or not key:
        typer.echo("não consegui deduzir semestre/sigla do caminho: use --semester e --key", err=True); raise typer.Exit(2)
    with _client() as c:
        try:
            _print_result(propose(c, pdf, semester, key, _schedule_llm()))
        except Exception as e:
            typer.echo(f"erro: {e}", err=True); raise typer.Exit(1)


@schedule_app.command("scan")
def schedule_scan_cmd(root: Optional[Path] = typer.Option(None, help="padrão: STUDYAGENT_SCHEDULE_ROOT ou ~/Obsidian Vault/UFMG")):
    """Procura Cronograma.pdf novos ou alterados e cria as propostas."""
    from .schedule_scan import scan
    root = root or Settings().schedule_root
    if not root.is_dir():
        typer.echo(f"pasta não encontrada: {root}", err=True); raise typer.Exit(2)
    with _client() as c:
        try:
            res = scan(c, root, _schedule_llm())
        except RuntimeError as e:
            typer.echo(f"erro: {e}", err=True); raise typer.Exit(1)
    for r in res:
        _print_result(r)
    typer.echo(f"{len(res)} cronograma(s) novo(s) ou alterado(s)" if res else "nada novo")


@schedule_app.command("watch")
def schedule_watch(root: Optional[Path] = None, interval: int = typer.Option(300, help="segundos entre varreduras")):
    """Fica vigiando a pasta (hash do PDF muda -> nova proposta)."""
    import time
    from .schedule_scan import scan
    root = root or Settings().schedule_root
    failed: set[str] = set()
    typer.echo(f"vigiando {root} a cada {interval}s")
    while True:
        try:
            with _client() as c:
                for r in scan(c, root, _schedule_llm(), failed):
                    _print_result(r)
        except (httpx.TransportError, OSError) as e:
            typer.echo(f"aguardando o daemon/pasta: {e}", err=True)
        time.sleep(interval)


@schedule_app.command("courses")
def schedule_courses():
    for c in _call("GET", "/courses"):
        mode = f"início {c['study_start']} k={c['study_k']}" if c["study_start"] else f"aula − {c['study_lead']} dias"
        typer.echo(f"{c['semester']}  {c['key']:<8} {c['name'] or '-':<36} estudo: {mode}")


@schedule_app.command("calendar")
def schedule_calendar(date_from: Optional[str] = typer.Option(None, "--from"), date_to: Optional[str] = typer.Option(None, "--to"),
                      course: Optional[str] = None):
    params = {k: v for k, v in {"from": date_from, "to": date_to, "course": course}.items() if v}
    for s in _call("GET", "/calendar", params=params):
        mark = "★" if s["highlight"] else " "
        typer.echo(f"{mark} {s['date']} {s['start'] or '     '}  {s['label']}" + (f"  [{s['flag']}]" if s["flag"] else ""))


@schedule_app.command("shift")
def schedule_shift(semester: str, start: Optional[str] = typer.Option(None, help="aaaa-mm-dd: dia em que a 1ª aula de cada matéria passa a ser estudada"),
                   course: Optional[str] = typer.Option(None, help="só esta sigla"), k: Optional[float] = typer.Option(None, help="ritmo (1 = igual ao semestre)"),
                   lead: Optional[int] = typer.Option(None, help="dias de antecedência (sem --start)"),
                   clear: bool = typer.Option(False, "--clear", help="volta a 'data da aula − folga'")):
    """Adapta os prazos dos estudos de um semestre a partir de uma data (ex.: começar o próximo semestre antes)."""
    body = {k_: v for k_, v in {"semester": semester, "key": course, "start": start, "k": k, "lead": lead}.items() if v is not None}
    if clear:
        body["clear"] = True
    r = _call("POST", "/schedule/shift", json=body)
    for c in r["courses"]:
        typer.echo(f"{c['course']}: {c['tasks_moved']} prazos ajustados (início {c['study_start'] or '—'}, k={c['k']}, folga {c['lead']})")


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
