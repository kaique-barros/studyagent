"""XP, nível, streak e freezes — funções puras (sem banco), calculadas do histórico.

Regras (decididas com o usuário):
  - cada tarefa concluída rende `base_xp`; atrasada (depois do prazo) rende 30% e quebra a streak;
  - bônus de streak: +0,5% por dia (teto em ~2 meses), só em tarefas no prazo; usa a streak do fim do dia anterior;
  - um dia (que vira às 04:00) VALE se: concluiu pelo menos 4 tarefas (2 em folga), concluiu todas as marcadas para ele,
    nenhuma conclusão foi atrasada, e havia pelo menos 24 tarefas programadas (abertas, com ou sem data);
  - dia que falha quebra a streak, a menos que haja um freeze (gasta sozinho); dia salvo por freeze não soma nem quebra;
  - freeze: 1 a cada 60 tarefas concluídas, guardando no máximo `freeze_cap`;
  - dia que vale rende um bônus de XP; XP e nível nunca diminuem (só há somas).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable


@dataclass(frozen=True)
class ProgressConfig:
    base_xp: int = 10
    late_factor: float = 0.3
    streak_step: float = 0.005
    streak_cap_days: int = 60
    daily_min: int = 4
    dayoff_min: int = 2
    programmed_goal: int = 24
    freeze_every: int = 60
    freeze_cap: int = 3
    day_bonus_xp: int = 20
    level_base: float = 50
    level_exp: float = 1.5


@dataclass(frozen=True)
class Completion:
    task_id: int
    day: str
    late: bool = False


@dataclass(frozen=True)
class DaySnap:
    day: str
    scheduled: tuple[int, ...] = ()      # tarefas marcadas para o dia (última foto do dia)
    programmed: int = 0                  # tarefas programadas (abertas + feitas no dia)
    dayoff: bool = False


def xp_for_level(n: int, cfg: ProgressConfig) -> int:
    return round(cfg.level_base * n ** cfg.level_exp)


def level_of(xp: float, cfg: ProgressConfig) -> int:
    n = 0
    while xp_for_level(n + 1, cfg) <= xp:
        n += 1
    return n


def _d(s: str) -> date:
    return date.fromisoformat(s)


def compute(completions: Iterable[Completion], snaps: Iterable[DaySnap], today: str, cfg: ProgressConfig = ProgressConfig()) -> dict[str, Any]:
    comps: dict[int, Completion] = {}
    for c in completions:
        comps.setdefault(c.task_id, c)                     # a primeira conclusão vale
    by_day: dict[str, list[Completion]] = defaultdict(list)
    for c in comps.values():
        by_day[c.day].append(c)
    snap_by_day = {s.day: s for s in snaps}
    done_day = {tid: c.day for tid, c in comps.items()}

    first_days = [d for d in list(by_day) + list(snap_by_day)]
    xp = 0.0
    streak = best = freezes = granted = total_done = 0
    history: list[dict[str, Any]] = []

    if first_days:
        d = _d(min(first_days))
        end = _d(today)
        while d < end:                                     # dias já fechados
            k = d.isoformat()
            snap = snap_by_day.get(k, DaySnap(k))
            todays = by_day.get(k, [])
            mult = 1 + cfg.streak_step * min(streak, cfg.streak_cap_days)
            for c in todays:
                xp += round(cfg.base_xp * (cfg.late_factor if c.late else mult))
            total_done += len(todays)
            earned = total_done // cfg.freeze_every
            freezes = min(cfg.freeze_cap, freezes + (earned - granted))
            granted = earned

            need = cfg.dayoff_min if snap.dayoff else cfg.daily_min
            reasons = []
            if len(todays) < need:
                reasons.append("min")
            if any(not (tid in done_day and done_day[tid] <= k) for tid in snap.scheduled):
                reasons.append("scheduled")
            if any(c.late for c in todays):
                reasons.append("late")
            if snap.programmed < cfg.programmed_goal:
                reasons.append("programmed")

            if not reasons:
                streak += 1
                best = max(best, streak)
                xp += cfg.day_bonus_xp
                status = "ok"
            elif freezes > 0:
                freezes -= 1
                status = "frozen"
            else:
                streak = 0
                status = "failed"
            history.append({"day": k, "status": status, "done": len(todays), "need": need, "reasons": reasons,
                            "dayoff": snap.dayoff})
            d += timedelta(days=1)

    # dia de hoje (ainda aberto): o XP já conta, a streak só muda quando o dia fecha
    mult = 1 + cfg.streak_step * min(streak, cfg.streak_cap_days)
    todays = by_day.get(today, [])
    for c in todays:
        xp += round(cfg.base_xp * (cfg.late_factor if c.late else mult))
    total_done += len(todays)
    earned = total_done // cfg.freeze_every
    freezes = min(cfg.freeze_cap, freezes + (earned - granted))

    snap = snap_by_day.get(today, DaySnap(today))
    need = cfg.dayoff_min if snap.dayoff else cfg.daily_min
    sched_done = sum(1 for tid in snap.scheduled if done_day.get(tid, "9999") <= today)
    week_from = (_d(today) - timedelta(days=6)).isoformat()
    lvl = level_of(xp, cfg)
    start_xp, next_xp = xp_for_level(lvl, cfg), xp_for_level(lvl + 1, cfg)
    return {
        "xp": round(xp), "level": lvl, "level_start_xp": start_xp, "next_level_xp": next_xp,
        "level_progress": max(0.0, min(1.0, (xp - start_xp) / max(1, next_xp - start_xp))),
        "streak": streak, "best_streak": best, "freezes": freezes, "freeze_cap": cfg.freeze_cap,
        "multiplier": round(mult, 4), "total_done": total_done,
        "week_done": sum(len(v) for k, v in by_day.items() if week_from <= k <= today),
        "today": {
            "day": today, "done": len(todays), "need": need, "dayoff": snap.dayoff,
            "scheduled": len(snap.scheduled), "scheduled_done": sched_done,
            "programmed": snap.programmed, "programmed_goal": cfg.programmed_goal,
            "late_today": any(c.late for c in todays),
            "on_track": (len(todays) >= need and sched_done == len(snap.scheduled)
                         and not any(c.late for c in todays) and snap.programmed >= cfg.programmed_goal),
        },
        "history": history[-30:],
    }
