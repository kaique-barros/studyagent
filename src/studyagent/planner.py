"""Planejador determinístico (sem LLM): distribui as tarefas abertas pelos dias.

Regras (do sistema de XP):
  - cada dia recebe `daily` tarefas (4); em dia de folga, `off` (2);
  - tarefa com prazo é planejada num dia <= prazo (o mais cedo possível, prazo mais próximo primeiro);
  - tarefa atrasada (prazo no passado) tem prioridade total e vai para os primeiros espaços livres;
  - o que já estava planejado e continua válido FICA onde está (o cronograma não muda à toa);
  - tarefas sem prazo (ou com prazo além do horizonte) preenchem os espaços que sobrarem.

Funções puras: recebem dados e devolvem o plano, sem tocar no banco.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any


@dataclass(frozen=True)
class PlanConfig:
    daily: int = 4
    off: int = 2
    horizon: int = 14
    programmed_goal: int = 24


@dataclass
class Move:
    task_id: int
    title: str
    frm: str | None
    to: str | None


@dataclass
class Plan:
    moves: list[Move] = field(default_factory=list)
    assignments: dict[int, str] = field(default_factory=dict)       # tarefa -> dia (dentro do horizonte)
    capacity: dict[str, int] = field(default_factory=dict)
    load: dict[str, int] = field(default_factory=dict)
    overloaded: dict[str, int] = field(default_factory=dict)        # dia -> tarefas acima da capacidade
    shortfall: dict[str, int] = field(default_factory=dict)         # dia -> tarefas que faltam para a capacidade
    overdue: list[int] = field(default_factory=list)
    planned_after_due: list[int] = field(default_factory=list)      # não coube antes do prazo
    programmed: int = 0
    programmed_goal: int = 24

    @property
    def programmed_ok(self) -> bool:
        return self.programmed >= self.programmed_goal

    def to_dict(self) -> dict[str, Any]:
        return {
            "moves": [{"task_id": m.task_id, "title": m.title, "from": m.frm, "to": m.to} for m in self.moves],
            "overloaded": self.overloaded, "shortfall": self.shortfall, "overdue": self.overdue,
            "planned_after_due": self.planned_after_due,
            "programmed": self.programmed, "programmed_goal": self.programmed_goal, "programmed_ok": self.programmed_ok,
        }


def make_plan(tasks: list[dict[str, Any]], dayoffs: set[str], today: str, cfg: PlanConfig = PlanConfig()) -> Plan:
    t0 = date.fromisoformat(today)
    iso = [(t0 + timedelta(days=i)).isoformat() for i in range(cfg.horizon)]
    last = iso[-1]
    cap = {s: (cfg.off if s in dayoffs else cfg.daily) for s in iso}
    load = {s: 0 for s in iso}
    assign: dict[int, str] = {}

    open_tasks = [t for t in tasks if t["status"] == "open"]
    key = lambda t: (t["due"] or "9999-12-31", t["id"])  # noqa: E731

    def free(s: str) -> bool:
        return load[s] < cap[s]

    def place(t: dict[str, Any], s: str) -> None:
        assign[t["id"]] = s
        load[s] += 1

    def earliest_free(upto: str | None) -> str | None:
        for s in iso:
            if upto is not None and s > upto:
                return None
            if free(s):
                return s
        return None

    # 1) atrasadas: primeiros espaços livres (se o horizonte estiver cheio, vão para hoje, acima da capacidade)
    overdue = sorted((t for t in open_tasks if t["due"] and t["due"] < today), key=key)
    for t in overdue:
        place(t, earliest_free(None) or iso[0])

    # 2) o que já estava planejado e continua válido fica onde está
    overdue_ids = {t["id"] for t in overdue}
    pinned = sorted(
        (t for t in open_tasks if t["id"] not in overdue_ids and t["planned_for"] in cap
         and (t["due"] is None or t["planned_for"] <= t["due"])),
        key=lambda t: (t["planned_for"], key(t)),
    )
    queue: list[dict[str, Any]] = []
    pinned_ids: set[int] = set()
    for t in pinned:
        if free(t["planned_for"]):
            place(t, t["planned_for"])
            pinned_ids.add(t["id"])
        else:
            queue.append(t)

    # 3) o resto em ordem de prazo (EDF); se um prazo não tem espaço, expulsa um planejado com prazo mais tardio
    queue += [t for t in open_tasks if t["id"] not in assign and t not in queue]
    queue.sort(key=key)
    keys = [key(t) for t in queue]
    by_id = {t["id"]: t for t in open_tasks}

    while queue:
        t = queue.pop(0)
        keys.pop(0)
        deadline = t["due"] if (t["due"] and t["due"] <= last) else None
        s = earliest_free(deadline if deadline else None)
        if s is not None:
            place(t, s)
            continue
        if deadline is None:
            continue                                   # sem espaço no horizonte: fica sem dia
        victims = [by_id[i] for i in pinned_ids if assign.get(i) and assign[i] <= deadline
                   and (by_id[i]["due"] is None or by_id[i]["due"] > deadline)]
        if victims:
            v = max(victims, key=lambda p: (p["due"] or "9999-12-31", assign[p["id"]]))
            day = assign.pop(v["id"])
            load[day] -= 1
            pinned_ids.discard(v["id"])
            place(t, day)
            pos = bisect.bisect(keys, key(v))
            queue.insert(pos, v)
            keys.insert(pos, key(v))
        else:
            place(t, deadline)                         # não cabe: planeja no dia do prazo, acima da capacidade

    # 4) movimentos = diferenças em relação ao que está no banco
    moves: list[Move] = []
    for t in sorted(open_tasks, key=key):
        new, cur = assign.get(t["id"]), t["planned_for"]
        if new == cur:
            continue
        if new is None and cur is not None and cur > last:
            continue                                   # planejada além do horizonte: não mexe
        moves.append(Move(t["id"], t["title"], cur, new))

    plan = Plan(moves=moves, assignments=assign, capacity=cap, load=load,
                overdue=[t["id"] for t in overdue], programmed=len(open_tasks), programmed_goal=cfg.programmed_goal)
    plan.overloaded = {s: load[s] - cap[s] for s in iso if load[s] > cap[s]}
    plan.shortfall = {s: cap[s] - load[s] for s in iso if load[s] < cap[s]}
    plan.planned_after_due = [i for i, s in assign.items() if by_id[i]["due"] and s > by_id[i]["due"]]
    return plan


def suggest_dayoffs(dayoffs: set[str], today: str, horizon: int, earliest: str, min_month: int, max_month: int) -> list[str]:
    """Meses tocados pelo horizonte com menos folgas que o mínimo: sugere datas (domingos, depois sábados, depois o fim do mês)."""
    t0 = date.fromisoformat(today)
    months = {((t0 + timedelta(days=i)).year, (t0 + timedelta(days=i)).month) for i in range(horizon)}
    out: list[str] = []
    for (y, m) in sorted(months):
        have = {d for d in dayoffs if d.startswith(f"{y:04d}-{m:02d}-")}
        need = min(min_month - len(have), max_month - len(have))
        if need <= 0:
            continue
        days = []
        d = date(y, m, 1)
        while d.month == m:
            if d.isoformat() >= earliest and d.isoformat() not in dayoffs:
                days.append(d)
            d += timedelta(days=1)
        order = ([d for d in days if d.weekday() == 6] + [d for d in days if d.weekday() == 5]
                 + sorted((d for d in days if d.weekday() < 5), reverse=True))
        out += sorted(d.isoformat() for d in order[:need])
    return out
