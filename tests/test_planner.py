from studyagent.planner import PlanConfig, make_plan, suggest_dayoffs

TODAY = "2026-11-21"


def T(i, due=None, planned=None, status="open"):
    return {"id": i, "title": f"T{i}", "status": status, "due": due, "planned_for": planned}


def assigned(plan):
    return plan.assignments


# ---------- planejador puro ----------
def test_respects_capacity_and_deadlines():
    tasks = [T(1, "2026-11-21"), T(2, "2026-11-21"), T(3, "2026-11-22")] + [T(i) for i in range(4, 12)]
    p = make_plan(tasks, set(), TODAY)
    assert all(p.load[d] <= p.capacity[d] for d in p.load)
    assert not p.planned_after_due and not p.overloaded
    assert assigned(p)[1] == assigned(p)[2] == "2026-11-21"
    assert assigned(p)[3] <= "2026-11-22"


def test_dayoff_has_smaller_capacity():
    tasks = [T(i) for i in range(1, 9)]
    p = make_plan(tasks, {"2026-11-21"}, TODAY)
    assert p.capacity["2026-11-21"] == 2
    assert sum(1 for d in assigned(p).values() if d == "2026-11-21") == 2


def test_overdue_first_and_stability():
    tasks = [T(1, "2026-11-10"), T(2, planned="2026-11-22"), T(3, planned="2026-11-22")]
    p = make_plan(tasks, set(), TODAY)
    assert assigned(p)[1] == "2026-11-21"
    assert 1 in p.overdue
    assert {m.task_id for m in p.moves} >= {1}
    assert 2 not in {m.task_id for m in p.moves}          # o que estava planejado e válido não mexe


def test_second_run_is_idempotent():
    tasks = [T(i, due="2026-11-24") for i in range(1, 6)] + [T(i) for i in range(6, 15)]
    p = make_plan(tasks, set(), TODAY)
    for m in p.moves:
        next(t for t in tasks if t["id"] == m.task_id)["planned_for"] = m.to
    assert make_plan(tasks, set(), TODAY).moves == []


def test_eviction_for_earlier_deadline():
    # 4 tarefas sem prazo fixadas hoje e uma com prazo hoje: a de prazo toma o lugar
    tasks = [T(i, planned=TODAY) for i in range(1, 5)] + [T(9, due=TODAY)]
    p = make_plan(tasks, set(), TODAY)
    assert assigned(p)[9] == TODAY and not p.overloaded


def test_overload_when_infeasible():
    tasks = [T(i, due=TODAY) for i in range(1, 7)]
    p = make_plan(tasks, set(), TODAY)
    assert p.overloaded == {TODAY: 2}


def test_horizon_and_programmed():
    tasks = [T(i) for i in range(1, 80)]
    p = make_plan(tasks, set(), TODAY, PlanConfig())
    assert sum(p.load.values()) == 14 * 4 and len(assigned(p)) == 56
    assert p.programmed == 79 and p.programmed_ok
    assert not make_plan([T(1)], set(), TODAY).programmed_ok


def test_closed_tasks_ignored():
    p = make_plan([T(1, status="done"), T(2, status="dropped")], set(), TODAY)
    assert p.moves == [] and p.programmed == 0


def test_suggest_dayoffs_min_per_month():
    s = suggest_dayoffs(set(), TODAY, 14, "2026-11-22", 4, 10)
    nov = [d for d in s if d.startswith("2026-11")]
    dec = [d for d in s if d.startswith("2026-12")]
    assert len(nov) == 4 and len(dec) == 4
    assert all(d >= "2026-11-22" for d in s)
    assert suggest_dayoffs({"2026-11-22", "2026-11-23", "2026-11-28", "2026-11-29"}, TODAY, 7, "2026-11-22", 4, 10) == []


# ---------- folgas (API) ----------
def H(env):
    return env.h("user")


def test_dayoff_rules(env):
    assert env.post("/dayoffs", json={"date": "2026-11-21"}, headers=H(env)).status_code == 422   # hoje não
    assert env.post("/dayoffs", json={"date": "2026-11-20"}, headers=H(env)).status_code == 422
    assert env.post("/dayoffs", json={"date": "2026-11-22"}, headers=H(env)).status_code == 201
    r = env.post("/dayoffs", json={"date": "2026-11-22"}, headers=H(env))
    assert r.json()["added"] is False                                                           # idempotente
    assert env.post("/dayoffs", json={"date": "2026-11-22"}, headers=env.h("viewer")).status_code == 403
    assert env.post("/dayoffs", json={"date": "2026-11-22"}, headers=env.h("planner")).status_code == 403


def test_dayoff_max_and_min(env):
    for d in range(22, 31):
        assert env.post("/dayoffs", json={"date": f"2026-11-{d}"}, headers=H(env)).status_code == 201
    assert env.post("/dayoffs", json={"date": "2026-11-30"}, headers=H(env)).status_code == 201   # 9
    assert env.post("/dayoffs", json={"date": "2026-11-30"}, headers=H(env)).json()["added"] is False
    # 10ª ok, 11ª recusada
    assert env.post("/dayoffs", json={"date": "2026-11-25"}, headers=H(env)).json()["added"] is False
    # preenche até 10 em dezembro para testar o teto
    for d in range(1, 11):
        assert env.post("/dayoffs", json={"date": f"2026-12-{d:02d}"}, headers=H(env)).status_code == 201
    assert env.post("/dayoffs", json={"date": "2026-12-11"}, headers=H(env)).status_code == 409
    # remoção: nov tem 9 (22..30); tirar até sobrar 4
    for d in range(22, 27):
        assert env.delete(f"/dayoffs/2026-11-{d}", headers=H(env)).status_code == 200
    assert env.delete("/dayoffs/2026-11-27", headers=H(env)).status_code == 409                 # ficaria abaixo de 4
    assert env.delete("/dayoffs/2026-11-01", headers=H(env)).status_code == 404


# ---------- planejador (propostas) ----------
def mk(env, **kw):
    return env.post("/tasks", json={"title": "Estudar", **kw}, headers=H(env)).json()


def props(env, kind):
    return [p for p in env.get("/proposals", params={"status": "pending"}, headers=H(env)).json() if p["kind"] == kind]


def test_plan_is_a_proposal_and_applies(env):
    ids = [mk(env)["id"] for _ in range(6)]
    assert all(t["planned_for"] is None for t in env.get("/tasks", headers=H(env)).json())   # nada muda sozinho
    r = env.post("/planner/run", headers=H(env)).json()
    assert r["moves"] == 6
    plan = props(env, "plan.apply")
    assert len(plan) == 1 and len(plan[0]["payload"]["moves"]) == 6
    # rodar de novo não duplica
    assert env.post("/planner/run", headers=H(env)).json()["created_proposals"] == []
    assert len(props(env, "plan.apply")) == 1
    out = env.post(f"/proposals/{plan[0]['id']}/approve", headers=H(env)).json()
    assert out["status"] == "applied"
    ts = {t["id"]: t for t in env.get("/tasks", headers=H(env)).json()}
    assert sorted(t["planned_for"] for t in ts.values()).count("2026-11-21") == 4
    assert env.post("/planner/run", headers=H(env)).json()["moves"] == 0                     # estável


def test_plan_stale_and_superseded(env):
    a = mk(env)["id"]; b = mk(env)["id"]
    env.post("/planner/run", headers=H(env))
    first = props(env, "plan.apply")[0]
    # a tarefa a muda depois do plano: o movimento dela é pulado, o de b é aplicado
    env.post(f"/tasks/{a}/complete", headers=H(env))
    out = env.post(f"/proposals/{first['id']}/approve", headers=H(env)).json()
    import json
    assert out["status"] == "applied" and a in json.loads(out["result"])["skipped"]
    # um novo plano substitui o pendente anterior
    mk(env)
    env.post("/planner/run", headers=H(env))
    pend = props(env, "plan.apply")
    assert len(pend) == 1 and pend[0]["id"] != first["id"]


def test_all_stale_when_everything_changed(env):
    a = mk(env)["id"]
    env.post("/planner/run", headers=H(env))
    pid = props(env, "plan.apply")[0]["id"]
    env.post(f"/tasks/{a}/complete", headers=H(env))
    assert env.post(f"/proposals/{pid}/approve", headers=H(env)).json()["status"] == "stale"


def test_near_due_triggers_replan(env):
    mk(env, due="2026-11-23")                                 # dentro de 3 dias
    assert props(env, "plan.apply")
    n = len(env.get("/proposals", headers=H(env)).json())
    mk(env, due="2026-12-20")                                 # distante: não dispara
    assert len(env.get("/proposals", headers=H(env)).json()) == n


def test_dayoff_suggestion_proposal_and_approve(env):
    env.post("/planner/run", headers=H(env))
    sug = props(env, "dayoff.add")
    assert len(sug) == 1 and sug[0]["source"] == "planner"
    assert env.post(f"/proposals/{sug[0]['id']}/approve", headers=env.h("planner")).status_code == 403
    out = env.post(f"/proposals/{sug[0]['id']}/approve", headers=H(env)).json()
    assert out["status"] == "applied"
    days = env.get("/dayoffs", headers=H(env)).json()
    assert len(days) >= 4 and all(d > "2026-11-21" for d in days)


def test_summary_programmed(env):
    for _ in range(3):
        mk(env)
    s = env.get("/summary", headers=H(env)).json()
    assert s["programmed"] == 3 and s["programmed_goal"] == 24


def test_preview_does_not_write(env):
    mk(env)
    before = len(env.get("/proposals", headers=H(env)).json())
    p = env.get("/planner/preview", headers=env.h("viewer")).json()
    assert p["moves"] and len(env.get("/proposals", headers=H(env)).json()) == before


def test_daily_plan_once_per_day(env, clock):
    from datetime import timedelta
    from studyagent.db import connect
    from studyagent.service import Service
    mk(env)
    conn = connect(env.settings.db_path)
    svc = Service(conn, env.settings, clock)
    assert svc.maybe_daily_plan() is True                     # primeira checagem do dia
    assert svc.maybe_daily_plan() is False
    clock.t += timedelta(days=1)
    assert svc.maybe_daily_plan() is True                     # dia seguinte
    assert svc.maybe_daily_plan() is False
