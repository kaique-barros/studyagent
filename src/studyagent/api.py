
import asyncio
import contextlib
import json
import sqlite3
from datetime import date
from typing import Annotated, Any, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__, projection
from .auth import Actor, authenticate
from .config import Settings
from .db import connect, migrate
from .models import DayoffIn, FeedbackIn, ShiftIn, ProposalIn, ReasonIn, RescheduleIn, TaskIn
from .service import Clock, Rejected, Service, system_clock


def create_app(settings: Settings | None = None, clock: Clock = system_clock) -> FastAPI:
    settings = settings or Settings()
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    boot = connect(settings.db_path)
    migrate(boot)
    boot.close()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(_planner_loop(settings, clock))
        try:
            yield
        finally:
            if task:
                task.cancel()

    app = FastAPI(title="studyagent", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.clock = clock

    @app.exception_handler(Rejected)
    async def _rejected(_: Request, exc: Rejected) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    def get_actor(conn: Conn, authorization: Annotated[str | None, Header()] = None) -> Actor:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "token ausente", headers={"WWW-Authenticate": "Bearer"})
        actor = authenticate(conn, authorization[7:].strip())
        if actor is None:
            raise HTTPException(401, "token inválido ou revogado", headers={"WWW-Authenticate": "Bearer"})
        return actor

    Who = Annotated[Actor, Depends(get_actor)]

    def svc(conn: sqlite3.Connection) -> Service:
        return Service(conn, settings, clock)

    def need(actor: Actor, perm: str) -> None:
        if not actor.can(perm):
            raise HTTPException(403, f"o escopo '{actor.scope}' não tem a permissão '{perm}'")

    def sync_projection(conn: sqlite3.Connection) -> None:
        try:
            projection.write(conn, settings, clock())
        except OSError as e:  # falha de escrita do arquivo não deve derrubar a API; o banco já foi gravado
            print(f"studyagent: não consegui gravar {settings.tasks_file}: {e}", flush=True)

    # ---------- básico ----------
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/me")
    def me(actor: Who) -> dict[str, Any]:
        return {"name": actor.name, "scope": actor.scope}

    @app.get("/summary")
    def summary(conn: Conn, actor: Who) -> dict[str, Any]:
        need(actor, "tasks:read")
        return svc(conn).summary()

    # ---------- tarefas ----------
    @app.get("/tasks")
    def list_tasks(conn: Conn, actor: Who, status: str | None = None,
                   date_from: Annotated[str | None, Query(alias="from")] = None,
                   date_to: Annotated[str | None, Query(alias="to")] = None) -> list[dict[str, Any]]:
        need(actor, "tasks:read")
        return svc(conn).list_tasks(status, date_from, date_to)

    @app.post("/tasks")
    def create_task(body: TaskIn, conn: Conn, actor: Who, response: Response) -> dict[str, Any]:
        task, created = svc(conn).create_task(actor, body)
        response.status_code = 201 if created else 200
        sync_projection(conn)
        return task

    @app.post("/tasks/{task_id}/start")
    def start(task_id: int, conn: Conn, actor: Who) -> dict[str, Any]:
        return svc(conn).start_task(actor, task_id)

    @app.post("/tasks/{task_id}/pause")
    def pause(task_id: int, conn: Conn, actor: Who) -> dict[str, Any]:
        return svc(conn).pause_task(actor, task_id)

    @app.get("/progress")
    def progress(conn: Conn, actor: Who) -> dict[str, Any]:
        need(actor, "tasks:read")
        return svc(conn).progress()

    @app.get("/snapshot")
    def snapshot(conn: Conn, actor: Who, before: int = 31, after: int = 45) -> dict[str, Any]:
        need(actor, "tasks:read")
        return svc(conn).snapshot(min(max(before, 0), 120), min(max(after, 0), 200))

    @app.post("/tasks/{task_id}/complete")
    def complete(task_id: int, conn: Conn, actor: Who) -> dict[str, Any]:
        task = svc(conn).complete_task(actor, task_id)
        sync_projection(conn)
        return task

    @app.post("/tasks/{task_id}/reschedule", status_code=202)
    def reschedule(task_id: int, body: RescheduleIn, conn: Conn, actor: Who) -> dict[str, Any]:
        """Mudar data vira proposta (atrito de propósito): confirme em /proposals/{id}/approve."""
        return svc(conn).create_proposal(actor, ProposalIn(kind="task.reschedule", task_id=task_id,
                                                           due=body.due, planned_for=body.planned_for, reason=body.reason))

    @app.post("/tasks/{task_id}/drop", status_code=202)
    def drop(task_id: int, body: ReasonIn, conn: Conn, actor: Who) -> dict[str, Any]:
        return svc(conn).create_proposal(actor, ProposalIn(kind="task.drop", task_id=task_id, reason=body.reason))

    # ---------- propostas ----------
    @app.get("/proposals")
    def list_proposals(conn: Conn, actor: Who, status: str | None = None) -> list[dict[str, Any]]:
        need(actor, "proposals:read")
        return svc(conn).list_proposals(status)

    @app.post("/proposals", status_code=201)
    def create_proposal(body: ProposalIn, conn: Conn, actor: Who) -> dict[str, Any]:
        return svc(conn).create_proposal(actor, body)

    @app.post("/proposals/{pid}/approve")
    def approve(pid: int, conn: Conn, actor: Who) -> dict[str, Any]:
        out = svc(conn).decide(actor, pid, True)
        sync_projection(conn)
        return out

    @app.post("/proposals/{pid}/reject")
    def reject(pid: int, conn: Conn, actor: Who) -> dict[str, Any]:
        return svc(conn).decide(actor, pid, False)

    # ---------- folgas e planejador ----------
    @app.get("/dayoffs")
    def list_dayoffs(conn: Conn, actor: Who) -> list[str]:
        need(actor, "tasks:read")
        return sorted(svc(conn).dayoffs())

    @app.post("/dayoffs", status_code=201)
    def add_dayoff(body: DayoffIn, conn: Conn, actor: Who) -> dict[str, Any]:
        return svc(conn).add_dayoff(actor, body.date)

    @app.delete("/dayoffs/{day}")
    def remove_dayoff(day: date, conn: Conn, actor: Who) -> dict[str, Any]:
        return svc(conn).remove_dayoff(actor, day)

    @app.get("/planner/preview")
    def planner_preview(conn: Conn, actor: Who) -> dict[str, Any]:
        need(actor, "tasks:read")
        return svc(conn).plan_preview()

    @app.post("/planner/run")
    def planner_run(conn: Conn, actor: Who) -> dict[str, Any]:
        need(actor, "planner:run")
        return svc(conn).plan_run("manual")

    # ---------- matérias, calendário e plano de estudo ----------
    @app.get("/courses")
    def courses(conn: Conn, actor: Who) -> list[dict[str, Any]]:
        need(actor, "tasks:read")
        return svc(conn).list_courses()

    @app.get("/calendar")
    def calendar(conn: Conn, actor: Who, course: str | None = None,
                 date_from: Annotated[str | None, Query(alias="from")] = None,
                 date_to: Annotated[str | None, Query(alias="to")] = None) -> list[dict[str, Any]]:
        need(actor, "tasks:read")
        return svc(conn).calendar(date_from, date_to, course)

    @app.post("/schedule/shift")
    def shift(body: ShiftIn, conn: Conn, actor: Who) -> dict[str, Any]:
        out = svc(conn).shift_schedule(actor, body.semester, body.key, body.start, body.clear, body.k, body.lead)
        sync_projection(conn)
        return out

    @app.post("/feedback", status_code=201)
    def feedback(body: FeedbackIn, conn: Conn, actor: Who) -> dict[str, Any]:
        return {"event_id": svc(conn).add_feedback(actor, body.kind, body.text, body.data)}

    # ---------- eventos ----------
    @app.get("/events")
    def events(conn: Conn, actor: Who, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        need(actor, "events:read")
        return svc(conn).events(after, limit)

    @app.get("/events/stream")
    async def stream(request: Request, conn: Conn, actor: Who, after: int = 0,
                     last_event_id: Annotated[str | None, Header()] = None) -> StreamingResponse:
        """SSE: reenvia o que faltou (a partir de `after` ou Last-Event-ID) e depois entrega em tempo real."""
        need(actor, "events:read")
        cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

        async def gen():
            nonlocal cursor
            yield "retry: 3000\n\n"
            idle = 0
            while not await request.is_disconnected():
                evs = await asyncio.to_thread(_fetch, settings, cursor)
                for e in evs:
                    cursor = e["id"]
                    yield f"id: {e['id']}\nevent: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
                idle = 0 if evs else idle + 1
                if idle and idle % 15 == 0:
                    yield ": keepalive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    return app


async def _planner_loop(settings: Settings, clock: Clock) -> None:
    """No primeiro boot do dia (e a cada checagem) planeja se ainda não planejou hoje."""
    def tick() -> None:
        conn = connect(settings.db_path)
        try:
            svc_ = Service(conn, settings, clock)
            svc_.progress_sync()                    # mantém a foto do dia em dia (o fechamento da streak usa a última)
            svc_.maybe_daily_plan()
        finally:
            conn.close()

    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception as e:  # o laço nunca morre por causa de um erro pontual
            print(f"studyagent: erro no planejador: {e}", flush=True)
        await asyncio.sleep(settings.planner_check_seconds)


def _fetch(settings: Settings, after: int) -> list[dict[str, Any]]:
    conn = connect(settings.db_path)
    try:
        return Service(conn, settings).events(after, 200)
    finally:
        conn.close()
