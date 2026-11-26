"""Regras de negócio do core. Toda mudança passa por aqui: valida invariantes, grava e emite evento."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Callable

from .auth import KINDS_OF_SCOPE, Actor
from .config import Settings
from .db import tx
from .models import ProposalIn, RescheduleIn, TaskIn
from .planner import PlanConfig, make_plan, suggest_dayoffs
from .schedule import study_due

Clock = Callable[[], datetime]


class Rejected(Exception):
    """A operação viola uma regra; `status` sugere o código HTTP."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def system_clock() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def day_key(now: datetime, start_hour: int) -> str:
    return (now - timedelta(hours=start_hour)).date().isoformat()


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


class Service:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, clock: Clock = system_clock):
        self.conn = conn
        self.settings = settings
        self.clock = clock

    # ---------- util ----------
    def now(self) -> str:
        return iso(self.clock())

    def today(self) -> str:
        return day_key(self.clock(), self.settings.day_start_hour)

    def emit(self, type_: str, payload: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO event(ts, type, payload) VALUES (?,?,?)",
            (self.now(), type_, json.dumps(payload, ensure_ascii=False, default=str)),
        )
        return int(cur.lastrowid)

    def get_task(self, task_id: int) -> dict[str, Any]:
        t = _row(self.conn.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone())
        if t is None:
            raise Rejected("tarefa não encontrada", 404)
        return t

    def _check_dates(self, due: date | None, planned: date | None) -> None:
        today = date.fromisoformat(self.today())
        for name, d in (("due", due), ("planned_for", planned)):
            if d is not None and not (today - timedelta(days=366) <= d <= today + timedelta(days=730)):
                raise Rejected(f"{name} fora da faixa plausível ({d})")

    # ---------- leitura ----------
    def list_tasks(self, status: str | None = None, date_from: str | None = None, date_to: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM task WHERE 1=1", []
        if status:
            sql += " AND status=?"; args.append(status)
        if date_from:
            sql += " AND COALESCE(planned_for, due, '9999') >= ?"; args.append(date_from)
        if date_to:
            sql += " AND COALESCE(planned_for, due, '0000') <= ?"; args.append(date_to)
        sql += " ORDER BY COALESCE(planned_for, due, '9999-12-31'), id"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def summary(self) -> dict[str, Any]:
        today = self.today()
        q = lambda sql, *a: self.conn.execute(sql, a).fetchone()[0]  # noqa: E731
        return {
            "today": today,
            "open_total": q("SELECT COUNT(*) FROM task WHERE status='open'"),
            "open_due_today": q("SELECT COUNT(*) FROM task WHERE status='open' AND COALESCE(planned_for, due)=?", today),
            "overdue": q("SELECT COUNT(*) FROM task WHERE status='open' AND due < ?", today),
            "done_today": self._done_on_day(today),
            "pending_proposals": q("SELECT COUNT(*) FROM proposal WHERE status='pending'"),
            "programmed": q("SELECT COUNT(*) FROM task WHERE status='open'"),
            "programmed_goal": self.settings.programmed_goal,
            "dayoff_today": today in self.dayoffs(),
        }

    def _done_on_day(self, day: str) -> int:
        # o "dia" vira às day_start_hour: conta pelo evento de conclusão (que guarda o `day`)
        n = 0
        for r in self.conn.execute("SELECT payload FROM event WHERE type='task.completed'"):
            if json.loads(r["payload"]).get("day") == day:
                n += 1
        return n

    def events(self, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM event WHERE id>? ORDER BY id LIMIT ?", (after, min(limit, 1000))).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "type": r["type"], "payload": json.loads(r["payload"])} for r in rows]

    def list_proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM proposal", []
        if status:
            sql += " WHERE status=?"; args.append(status)
        sql += " ORDER BY id"
        out = []
        for r in self.conn.execute(sql, args):
            d = dict(r); d["payload"] = json.loads(d["payload"]); out.append(d)
        return out

    # ---------- criar / atualizar tarefas ----------
    def _kind_for(self, actor: Actor, payload: TaskIn) -> str:
        allowed = KINDS_OF_SCOPE.get(actor.scope, set())
        kind = payload.kind or sorted(allowed)[0]
        if kind not in allowed:
            raise Rejected(f"o escopo '{actor.scope}' não pode criar tarefas do tipo '{kind}'", 403)
        return kind

    def create_task(self, actor: Actor, p: TaskIn) -> tuple[dict[str, Any], bool]:
        """Cria (201) ou, se `source_ref` já existe na mesma origem, atualiza (200). Retorna (tarefa, criada)."""
        if not actor.can("tasks:create"):
            raise Rejected("sem permissão para criar tarefas", 403)
        kind = self._kind_for(actor, p)
        self._check_dates(p.due, p.planned_for)
        source = actor.source
        with tx(self.conn):
            existing = None
            if p.source_ref:
                existing = self.conn.execute("SELECT * FROM task WHERE source=? AND source_ref=?", (source, p.source_ref)).fetchone()
            if existing is None:
                now = self.now()
                cur = self.conn.execute(
                    "INSERT INTO task(title, course, due, planned_for, est_minutes, kind, source, source_ref, tag, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (p.title, p.course, _d(p.due), _d(p.planned_for), p.est_minutes, kind, source, p.source_ref, p.tag, now, now),
                )
                task = self.get_task(int(cur.lastrowid))
                self.emit("task.created", {"task_id": task["id"], "title": task["title"], "source": source, "due": task["due"]})
                created = True
            else:
                before = dict(existing)
                task = self._update_from_source(before, p, source)
                created = False
                if task["updated_at"] == before["updated_at"]:
                    return task, False                   # nada mudou: não replaneja
        self._near_due_trigger(task)
        return task, created

    def _update_from_source(self, task: dict[str, Any], p: TaskIn, source: str) -> dict[str, Any]:
        # tarefa já concluída ou descartada não é reaberta pela origem
        if task["status"] != "open":
            return task
        new = {"title": p.title, "course": p.course, "due": _d(p.due), "est_minutes": p.est_minutes, "tag": p.tag}
        if p.planned_for is not None:
            new["planned_for"] = _d(p.planned_for)
        changed = {k: v for k, v in new.items() if task.get(k) != v}
        if not changed:
            return task
        sets = ", ".join(f"{k}=?" for k in changed)
        self.conn.execute(f"UPDATE task SET {sets}, updated_at=? WHERE id=?", (*changed.values(), self.now(), task["id"]))
        self.emit("task.updated", {"task_id": task["id"], "source": source, "changed": {k: {"from": task.get(k), "to": v} for k, v in changed.items()}})
        return self.get_task(task["id"])

    # ---------- concluir ----------
    def complete_task(self, actor: Actor, task_id: int) -> dict[str, Any]:
        if not actor.can("tasks:complete"):
            raise Rejected("sem permissão para concluir tarefas", 403)
        with tx(self.conn):
            task = self.get_task(task_id)
            if task["status"] == "done":
                return task                              # idempotente: não gera evento nem XP de novo
            if task["status"] == "dropped":
                raise Rejected("tarefa descartada não pode ser concluída", 409)
            now = self.clock()
            day = day_key(now, self.settings.day_start_hour)
            late = bool(task["due"]) and task["due"] < day
            self.conn.execute("UPDATE task SET status='done', done_at=?, updated_at=? WHERE id=?", (iso(now), iso(now), task_id))
            self.emit("task.completed", {
                "task_id": task_id, "title": task["title"], "due": task["due"], "tag": task["tag"], "kind": task["kind"],
                "course": task["course"], "day": day, "late": late, "verification": task["verification"],
            })
            return self.get_task(task_id)

    # ---------- propostas ----------
    def create_proposal(self, actor: Actor, p: ProposalIn) -> dict[str, Any]:
        is_request = actor.can("tasks:request")
        if not (actor.can("proposals:create") or is_request):
            raise Rejected("sem permissão para propor mudanças", 403)
        task_id, task_version, payload = None, None, {}
        if p.kind == "task.create":
            if p.task is None:
                raise Rejected("task.create exige o campo 'task'")
            kind = self._kind_for(actor, p.task)
            self._check_dates(p.task.due, p.task.planned_for)
            payload = {**p.task.model_dump(mode="json"), "kind": kind}
        elif p.kind == "schedule.import":
            if p.schedule is None:
                raise Rejected("schedule.import exige o campo 'schedule'")
            payload = p.schedule.model_dump(mode="json")
        else:
            if p.task_id is None:
                raise Rejected(f"{p.kind} exige 'task_id'")
            task = self.get_task(p.task_id)
            if task["status"] != "open":
                raise Rejected("só tarefas abertas podem mudar", 409)
            task_id, task_version = task["id"], task["updated_at"]
            if p.kind == "task.reschedule":
                r = RescheduleIn(due=p.due, planned_for=p.planned_for, reason=p.reason)
                self._check_dates(r.due, r.planned_for)
                # prazo de tarefa vinda de uma origem externa é da origem: ninguém muda
                if r.due is not None and task["source"] in ("moodle",):
                    raise Rejected("o prazo de tarefas do Moodle vem do Moodle; mude 'planned_for'", 403)
                payload = {"due": _d(r.due), "planned_for": _d(r.planned_for)}
        with tx(self.conn):
            if p.kind == "schedule.import":                 # um cronograma novo da mesma matéria substitui o pendente
                for old in self.list_proposals("pending"):
                    if old["kind"] == "schedule.import" and (old["payload"]["semester"], old["payload"]["key"]) == (payload["semester"], payload["key"]):
                        self.conn.execute("UPDATE proposal SET status='stale', decided_at=?, result='substituída por um cronograma mais novo' WHERE id=?", (self.now(), old["id"]))
            cur = self.conn.execute(
                "INSERT INTO proposal(source, kind, payload, reason, task_id, task_version, created_at) VALUES (?,?,?,?,?,?,?)",
                (actor.source if actor.scope != "user" else "manual", p.kind, json.dumps(payload, ensure_ascii=False), p.reason, task_id, task_version, self.now()),
            )
            pid = int(cur.lastrowid)
            self.emit("proposal.created", {"proposal_id": pid, "kind": p.kind, "task_id": task_id, "source": actor.source, "reason": p.reason})
        return self.get_proposal(pid)

    def get_proposal(self, pid: int) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM proposal WHERE id=?", (pid,)).fetchone()
        if r is None:
            raise Rejected("proposta não encontrada", 404)
        d = dict(r); d["payload"] = json.loads(d["payload"]); return d

    def decide(self, actor: Actor, pid: int, approve: bool) -> dict[str, Any]:
        if not actor.can("proposals:decide"):
            raise Rejected("só o usuário decide propostas", 403)
        with tx(self.conn):
            prop = self.get_proposal(pid)
            if prop["status"] != "pending":
                raise Rejected(f"proposta já está '{prop['status']}'", 409)
            now = self.now()
            if not approve:
                self.conn.execute("UPDATE proposal SET status='rejected', decided_at=? WHERE id=?", (now, pid))
                self.emit("proposal.rejected", {"proposal_id": pid, "kind": prop["kind"], "task_id": prop["task_id"]})
                return self.get_proposal(pid)
            try:
                result = self._apply(prop)
            except Rejected as e:
                # a tarefa mudou desde a proposta (ou ficou inválida): não aplica
                self.conn.execute("UPDATE proposal SET status='stale', decided_at=?, result=? WHERE id=?", (now, str(e), pid))
                self.emit("proposal.stale", {"proposal_id": pid, "reason": str(e)})
                return self.get_proposal(pid)
            self.conn.execute("UPDATE proposal SET status='applied', decided_at=?, result=? WHERE id=?", (now, json.dumps(result), pid))
            self.emit("proposal.approved", {"proposal_id": pid, "kind": prop["kind"], "task_id": result.get("task_id")})
            out = self.get_proposal(pid)
        if prop["kind"] == "task.create" and result.get("task_id"):
            self._near_due_trigger(self.get_task(result["task_id"]))
        elif prop["kind"] == "schedule.import":
            self._replan("cronograma")
        return out

    def _apply(self, prop: dict[str, Any]) -> dict[str, Any]:
        k, payload = prop["kind"], prop["payload"]
        if k == "schedule.import":
            return self._apply_schedule(payload)
        if k == "plan.apply":
            return self._apply_plan(payload)
        if k == "dayoff.add":
            return self._apply_dayoffs(payload)
        if k == "task.create":
            now = self.now()
            self._check_dates(_pd(payload.get("due")), _pd(payload.get("planned_for")))
            ref = payload.get("source_ref")
            if ref and self.conn.execute("SELECT 1 FROM task WHERE source=? AND source_ref=?", (prop["source"], ref)).fetchone():
                raise Rejected("já existe uma tarefa com esse source_ref")
            cur = self.conn.execute(
                "INSERT INTO task(title, course, due, planned_for, est_minutes, kind, source, source_ref, tag, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (payload["title"], payload.get("course"), payload.get("due"), payload.get("planned_for"), payload.get("est_minutes"),
                 payload["kind"], prop["source"], ref, payload.get("tag"), now, now),
            )
            tid = int(cur.lastrowid)
            self.emit("task.created", {"task_id": tid, "title": payload["title"], "source": prop["source"], "due": payload.get("due")})
            return {"task_id": tid}
        task = self.get_task(prop["task_id"])
        if task["status"] != "open" or task["updated_at"] != prop["task_version"]:
            raise Rejected("a tarefa mudou desde a proposta")
        now = self.now()
        if k == "task.reschedule":
            sets, args = [], []
            for f in ("due", "planned_for"):
                if payload.get(f):
                    sets.append(f"{f}=?"); args.append(payload[f])
            self.conn.execute(f"UPDATE task SET {', '.join(sets)}, updated_at=? WHERE id=?", (*args, now, task["id"]))
            self.emit("task.updated", {"task_id": task["id"], "source": "proposal", "changed": {k: v for k, v in payload.items() if v}})
        elif k == "task.drop":
            self.conn.execute("UPDATE task SET status='dropped', updated_at=? WHERE id=?", (now, task["id"]))
            self.emit("task.dropped", {"task_id": task["id"], "title": task["title"]})
        return {"task_id": task["id"]}


    # ---------- feedback do agente ----------
    def add_feedback(self, actor: Actor, kind: str, text: str, data: dict[str, Any] | None) -> int:
        if not actor.can("feedback:create"):
            raise Rejected("sem permissão para publicar feedback", 403)
        with tx(self.conn):
            return self.emit("agent.feedback", {"kind": kind, "text": text, "data": data or {}, "source": actor.source})

    # ---------- folgas ----------
    def dayoffs(self) -> set[str]:
        return {r["date"] for r in self.conn.execute("SELECT date FROM dayoff")}

    def _month_count(self, month: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM dayoff WHERE date LIKE ?", (month + "-%",)).fetchone()[0]

    def _earliest_dayoff(self) -> str:
        return (date.fromisoformat(self.today()) + timedelta(days=self.settings.dayoff_lead_days)).isoformat()

    def _insert_dayoff(self, day: str, source: str) -> bool:
        """Valida e grava; devolve False se já existia. Levanta Rejected se violar uma regra."""
        if day < self._earliest_dayoff():
            raise Rejected(f"folga só pode ser criada com {self.settings.dayoff_lead_days} dia(s) de antecedência (a partir de {self._earliest_dayoff()})")
        if day > (date.fromisoformat(self.today()) + timedelta(days=366)).isoformat():
            raise Rejected("data longe demais")
        if self.conn.execute("SELECT 1 FROM dayoff WHERE date=?", (day,)).fetchone():
            return False
        if self._month_count(day[:7]) >= self.settings.dayoff_max_month:
            raise Rejected(f"máximo de {self.settings.dayoff_max_month} folgas em {day[:7]}", 409)
        self.conn.execute("INSERT INTO dayoff(date, source, created_at) VALUES (?,?,?)", (day, source, self.now()))
        self.emit("dayoff.added", {"date": day, "source": source})
        return True

    def add_dayoff(self, actor: Actor, day: date) -> dict[str, Any]:
        if not actor.can("dayoffs:write"):
            raise Rejected("sem permissão para mudar folgas", 403)
        with tx(self.conn):
            added = self._insert_dayoff(day.isoformat(), "manual")
        if added:
            self._replan("dayoff")
        return {"date": day.isoformat(), "added": added}

    def remove_dayoff(self, actor: Actor, day: date) -> dict[str, Any]:
        if not actor.can("dayoffs:write"):
            raise Rejected("sem permissão para mudar folgas", 403)
        d = day.isoformat()
        with tx(self.conn):
            if not self.conn.execute("SELECT 1 FROM dayoff WHERE date=?", (d,)).fetchone():
                raise Rejected("essa data não é folga", 404)
            if d < self._earliest_dayoff():
                raise Rejected("folga de hoje ou do passado não pode ser removida")
            n = self._month_count(d[:7])
            if n <= self.settings.dayoff_min_month:
                raise Rejected(f"o mês precisa de pelo menos {self.settings.dayoff_min_month} folgas", 409)
            self.conn.execute("DELETE FROM dayoff WHERE date=?", (d,))
            self.emit("dayoff.removed", {"date": d})
        self._replan("dayoff")
        return {"date": d, "removed": True}

    def _apply_dayoffs(self, payload: dict[str, Any]) -> dict[str, Any]:
        added, skipped = [], []
        for d in payload.get("dates", []):
            try:
                if self._insert_dayoff(d, "planner"):
                    added.append(d)
            except Rejected:
                skipped.append(d)
        if not added and skipped:
            raise Rejected("nenhuma das folgas sugeridas pôde ser criada")
        return {"added": added, "skipped": skipped}

    # ---------- planejador ----------
    def _plan_cfg(self) -> PlanConfig:
        s = self.settings
        return PlanConfig(s.daily_target, s.dayoff_target, s.horizon_days, s.programmed_goal)

    def _build_plan(self):
        tasks = self.list_tasks("open")
        return make_plan(tasks, self.dayoffs(), self.today(), self._plan_cfg())

    def plan_preview(self) -> dict[str, Any]:
        plan = self._build_plan()
        out = plan.to_dict()
        out["capacity"] = plan.capacity
        out["load"] = plan.load
        out["dayoff_suggestions"] = suggest_dayoffs(
            self.dayoffs(), self.today(), self.settings.horizon_days, self._earliest_dayoff(),
            self.settings.dayoff_min_month, self.settings.dayoff_max_month)
        return out

    def _get_meta(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def _supersede(self, kind: str, keep_id: int | None = None) -> None:
        self.conn.execute(
            "UPDATE proposal SET status='stale', decided_at=?, result='substituída por um plano mais novo' "
            "WHERE status='pending' AND kind=? AND id IS NOT ?", (self.now(), kind, keep_id))

    def plan_run(self, reason: str = "manual") -> dict[str, Any]:
        """Calcula o plano e o registra como PROPOSTA (nada muda até você aprovar)."""
        created: list[int] = []
        with tx(self.conn):
            today = self.today()
            plan = self._build_plan()
            by_id = {t["id"]: t for t in self.list_tasks("open")}
            moves = [{"task_id": m.task_id, "title": m.title, "from": m.frm, "to": m.to, "task_version": by_id[m.task_id]["updated_at"]}
                     for m in plan.moves]
            sig = json.dumps([[m["task_id"], m["to"]] for m in moves])
            pending = [p for p in self.list_proposals("pending") if p["kind"] == "plan.apply"]
            if not moves:
                self._supersede("plan.apply")
            elif not any(json.dumps([[m["task_id"], m["to"]] for m in p["payload"]["moves"]]) == sig for p in pending):
                self._supersede("plan.apply")
                stats = {k: v for k, v in plan.to_dict().items() if k != "moves"}
                cur = self.conn.execute(
                    "INSERT INTO proposal(source, kind, payload, reason, created_at) VALUES ('planner','plan.apply',?,?,?)",
                    (json.dumps({"moves": moves, "stats": stats}, ensure_ascii=False), f"replanejamento ({reason})", self.now()))
                pid = int(cur.lastrowid); created.append(pid)
                self.emit("plan.proposed", {"proposal_id": pid, "moves": len(moves), "reason": reason,
                                            "overloaded": plan.overloaded, "overdue": plan.overdue})
            dates = suggest_dayoffs(self.dayoffs(), today, self.settings.horizon_days, self._earliest_dayoff(),
                                    self.settings.dayoff_min_month, self.settings.dayoff_max_month)
            pend_d = [p for p in self.list_proposals("pending") if p["kind"] == "dayoff.add"]
            if not dates:
                self._supersede("dayoff.add")
            elif not any(p["payload"].get("dates") == dates for p in pend_d):
                self._supersede("dayoff.add")
                cur = self.conn.execute(
                    "INSERT INTO proposal(source, kind, payload, reason, created_at) VALUES ('planner','dayoff.add',?,?,?)",
                    (json.dumps({"dates": dates}), f"faltam folgas (mínimo {self.settings.dayoff_min_month} por mês)", self.now()))
                pid = int(cur.lastrowid); created.append(pid)
                self.emit("proposal.created", {"proposal_id": pid, "kind": "dayoff.add", "task_id": None, "source": "planner", "reason": "folgas mínimas"})
            self.conn.execute("INSERT INTO meta(key,value) VALUES('last_plan_day',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (today,))
        return {"created_proposals": created, "moves": len(moves), "plan": plan.to_dict(), "dayoff_suggestions": dates}

    def _replan(self, reason: str) -> None:
        if self.settings.planner_enabled:
            self.plan_run(reason)

    def _near_due_trigger(self, task: dict[str, Any]) -> None:
        if task["status"] == "open" and task["due"] and \
                task["due"] <= (date.fromisoformat(self.today()) + timedelta(days=self.settings.near_due_days)).isoformat():
            self._replan("prazo próximo")

    def maybe_daily_plan(self) -> bool:
        """Roda no primeiro boot/checagem do dia."""
        if not self.settings.planner_enabled or self._get_meta("last_plan_day") == self.today():
            return False
        self.plan_run("início do dia")
        return True

    def _apply_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        applied, skipped = [], []
        today = date.fromisoformat(self.today())
        for m in payload["moves"]:
            row = self.conn.execute("SELECT * FROM task WHERE id=?", (m["task_id"],)).fetchone()
            if row is None or row["status"] != "open" or row["updated_at"] != m["task_version"]:
                skipped.append(m["task_id"]); continue
            self._check_dates(None, _pd(m["to"]))
            self.conn.execute("UPDATE task SET planned_for=?, updated_at=? WHERE id=?", (m["to"], self.now(), row["id"]))
            applied.append(m["task_id"])
        if not applied:
            raise Rejected("todas as tarefas do plano mudaram desde a proposta")
        self.emit("plan.applied", {"applied": applied, "skipped": skipped})
        return {"applied": applied, "skipped": skipped}

    # ---------- cronogramas (matérias e aulas) ----------
    def list_courses(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM course ORDER BY semester DESC, key")]

    def calendar(self, date_from: str | None = None, date_to: str | None = None, key: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT s.*, c.key AS course, c.semester FROM session s JOIN course c ON c.id=s.course_id WHERE 1=1")
        args: list[Any] = []
        if date_from:
            sql += " AND s.date >= ?"; args.append(date_from)
        if date_to:
            sql += " AND s.date <= ?"; args.append(date_to)
        if key:
            sql += " AND c.key = ?"; args.append(key)
        sql += " ORDER BY s.date, s.start, c.key"
        out = []
        for r in self.conn.execute(sql, args):
            d = dict(r)
            d["highlight"] = d["kind"] in ("exam", "holiday", "no_class") or bool(d["flag"])
            out.append(d)
        return out

    @staticmethod
    def _study_title(s: dict[str, Any]) -> str:
        t = " ".join((s.get("title") or s.get("topic") or "aula").split()).replace("#", "").replace("@{", "@")
        base = f"Estudar: {t}"
        return base if len(base) <= 200 else base[:197].rstrip() + "..."

    def _ensure_study_task(self, course: dict[str, Any], s: dict[str, Any], first: str, seen: set[str]) -> tuple[int, str]:
        ref = f"{course['semester']}:{course['key']}:" + (str(s["n"]) if s.get("n") is not None else f"d{s['date']}")
        if ref in seen:
            ref += ":" + s["date"]
        seen.add(ref)
        due = study_due(s["date"], first, course["study_lead"], course["study_start"], course["study_k"])
        title = self._study_title(s)
        now = self.now()
        ex = self.conn.execute("SELECT * FROM task WHERE source='schedule' AND source_ref=?", (ref,)).fetchone()
        if ex is None:
            cur = self.conn.execute(
                "INSERT INTO task(title, course, due, kind, source, source_ref, created_at, updated_at) VALUES (?,?,?,'study','schedule',?,?,?)",
                (title, course["key"], due, ref, now, now))
            tid = int(cur.lastrowid)
            self.emit("task.created", {"task_id": tid, "title": title, "source": "schedule", "due": due})
            return tid, "created"
        if ex["status"] == "open" and (ex["title"], ex["due"], ex["course"]) != (title, due, course["key"]):
            self.conn.execute("UPDATE task SET title=?, due=?, course=?, updated_at=? WHERE id=?", (title, due, course["key"], now, ex["id"]))
            self.emit("task.updated", {"task_id": ex["id"], "source": "schedule", "changed": {"due": {"from": ex["due"], "to": due}}})
            return ex["id"], "updated"
        return ex["id"], "same"

    def _apply_schedule(self, p: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        sem, key = p["semester"], p["key"]
        row = self.conn.execute("SELECT * FROM course WHERE semester=? AND key=?", (sem, key)).fetchone()
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO course(semester, key, code, name, section, professor, source_hash, imported_at) VALUES (?,?,?,?,?,?,?,?)",
                (sem, key, p.get("code"), p.get("name"), p.get("section"), p.get("professor"), p["source_hash"], now))
            cid = int(cur.lastrowid)
        else:
            cid = row["id"]
            self.conn.execute("UPDATE course SET code=?, name=?, section=?, professor=?, source_hash=?, imported_at=? WHERE id=?",
                              (p.get("code"), p.get("name"), p.get("section"), p.get("professor"), p["source_hash"], now, cid))
        course = dict(self.conn.execute("SELECT * FROM course WHERE id=?", (cid,)).fetchone())
        sessions = [dict(s) for s in p["sessions"]]
        classes = [s["date"] for s in sessions if s["kind"] == "class"]
        first = min(classes) if classes else None
        existing = {(r["date"], r["start"]): dict(r) for r in self.conn.execute("SELECT * FROM session WHERE course_id=?", (cid,))}
        seen_keys: set[tuple[str, str]] = set()
        seen_refs: set[str] = set()
        linked: set[int] = set()                                  # tarefas que continuam ligadas a alguma aula (mesmo que a data tenha mudado)
        stats = {"created": 0, "updated": 0, "tasks_created": 0, "tasks_updated": 0, "removed": 0, "tasks_dropped": 0}
        for s in sessions:
            k = (s["date"], s.get("start") or "")
            if k in seen_keys:
                continue
            seen_keys.add(k)
            task_id = None
            old = existing.get(k)
            if s["kind"] == "class" and first:
                task_id, what = self._ensure_study_task(course, s, first, seen_refs)
                linked.add(task_id)
                if what == "created":
                    stats["tasks_created"] += 1
                elif what == "updated":
                    stats["tasks_updated"] += 1
            elif old and old["task_id"] and old["task_id"] not in linked:
                self._drop_open_task(old["task_id"], stats)       # a linha deixou de ser aula: a tarefa aberta cai
            vals = (s.get("n"), s.get("end") or "", s["kind"], s.get("flag"), s["title"], s.get("topic"), s["label"], task_id)
            if old:
                self.conn.execute("UPDATE session SET n=?, end=?, kind=?, flag=?, title=?, topic=?, label=?, task_id=? WHERE id=?", (*vals, old["id"]))
                stats["updated"] += 1
            else:
                self.conn.execute(
                    "INSERT INTO session(course_id, date, start, n, end, kind, flag, title, topic, label, task_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, s["date"], s.get("start") or "", *vals))
                stats["created"] += 1
        for k, old in existing.items():
            if k not in seen_keys:
                if old["task_id"] and old["task_id"] not in linked:
                    self._drop_open_task(old["task_id"], stats)
                self.conn.execute("DELETE FROM session WHERE id=?", (old["id"],))
                stats["removed"] += 1
        self.emit("schedule.imported", {"semester": sem, "key": key, "sessions": len(seen_keys), **stats, "warnings": p.get("warnings", [])[:10]})
        return {"course": key, "semester": sem, "sessions": len(seen_keys), **stats}

    def _drop_open_task(self, task_id: int, stats: dict[str, int]) -> None:
        t = self.conn.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
        if t and t["status"] == "open":
            self.conn.execute("UPDATE task SET status='dropped', updated_at=? WHERE id=?", (self.now(), task_id))
            self.emit("task.dropped", {"task_id": task_id, "title": t["title"]})
            stats["tasks_dropped"] += 1

    def shift_schedule(self, actor: Actor, semester: str, key: str | None, start: date | None, clear: bool,
                       k: float | None, lead: int | None) -> dict[str, Any]:
        """Adapta os PRAZOS dos estudos (não as aulas): a 1ª aula de cada matéria passa a cair em `start`, mantendo o
        espaçamento (x k). Sem `start`, usa data da aula - folga. Tarefas já concluídas não mudam."""
        if not actor.can("schedule:shift"):
            raise Rejected("só o usuário adapta o plano de estudo", 403)
        changed = []
        with tx(self.conn):
            sql, args = "SELECT * FROM course WHERE semester=?", [semester]
            if key:
                sql += " AND key=?"; args.append(key)
            courses = [dict(r) for r in self.conn.execute(sql, args)]
            if not courses:
                raise Rejected("matéria/semestre não encontrado", 404)
            if start is not None:
                self._check_dates(start, None)
            for c in courses:
                new_start = None if clear else (start.isoformat() if start else c["study_start"])
                new_k = k if k is not None else c["study_k"]
                new_lead = lead if lead is not None else c["study_lead"]
                self.conn.execute("UPDATE course SET study_start=?, study_k=?, study_lead=? WHERE id=?", (new_start, new_k, new_lead, c["id"]))
                c.update(study_start=new_start, study_k=new_k, study_lead=new_lead)
                rows = self.conn.execute(
                    "SELECT s.date, t.id AS tid, t.due, t.status FROM session s JOIN task t ON t.id=s.task_id "
                    "WHERE s.course_id=? AND s.kind='class' ORDER BY s.date", (c["id"],)).fetchall()
                if not rows:
                    continue
                first, n = rows[0]["date"], 0
                for r in rows:
                    due = study_due(r["date"], first, new_lead, new_start, new_k)
                    if r["status"] == "open" and r["due"] != due:
                        self.conn.execute("UPDATE task SET due=?, updated_at=? WHERE id=?", (due, self.now(), r["tid"]))
                        n += 1
                changed.append({"course": c["key"], "tasks_moved": n, "study_start": new_start, "k": new_k, "lead": new_lead})
            self.emit("schedule.shifted", {"semester": semester, "courses": changed})
        self._replan("plano de estudo adaptado")
        return {"semester": semester, "courses": changed}


def _d(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _pd(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None
