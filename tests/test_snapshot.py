from datetime import timedelta


def mk(env, title, scope="user", **kw):
    r = env.post("/tasks", json={"title": title, **kw}, headers=env.h(scope))
    assert r.status_code in (200, 201), r.text
    return r.json()


def snap(env):
    r = env.get("/snapshot", headers=env.h("viewer"))
    assert r.status_code == 200, r.text
    return r.json()


def test_buckets_are_disjoint_and_late_flagged(env):
    a = mk(env, "hoje", due="2026-11-21")
    b = mk(env, "atrasada", due="2026-11-18")
    c = mk(env, "amanha", due="2026-11-22")
    d = mk(env, "semana", due="2026-11-26")
    e = mk(env, "quinze", due="2026-12-03")
    s = snap(env)["tasks"]
    ids = {k: {t["id"] for t in v} for k, v in s.items()}
    assert ids["today"] == {a["id"], b["id"]} and ids["tomorrow"] == {c["id"]}
    assert ids["week"] == {d["id"]} and ids["fifteen"] == {e["id"]}
    assert next(t for t in s["today"] if t["id"] == b["id"])["late"] is True


def test_delivery_ribbon_only_for_non_study_with_due(env):
    m = mk(env, "entrega", due="2026-11-25", kind="moodle", scope="moodle")
    st = mk(env, "estudo", due="2026-11-25", kind="study")
    sd = mk(env, "sem prazo", kind="manual")
    s = snap(env)["tasks"]
    by = {t["id"]: t for t in s["week"] + s["undated"]}
    assert by[m["id"]]["delivery"] is True and by[st["id"]]["delivery"] is False and by[sd["id"]]["delivery"] is False


def test_start_pause_complete_accumulates_time(env, clock):
    t = mk(env, "x", due="2026-11-21")
    assert env.post(f"/tasks/{t['id']}/start", headers=env.h("user")).status_code == 200
    clock.t += timedelta(seconds=90)
    s = snap(env)
    assert s["active"]["id"] == t["id"] and s["active"]["spent_seconds"] == 90
    r = env.post(f"/tasks/{t['id']}/complete", headers=env.h("user")).json()
    assert r["spent_seconds"] == 90 and r["started_at"] is None


def test_start_pauses_previous_and_viewer_cannot_start(env, clock):
    a, b = mk(env, "a", due="2026-11-21"), mk(env, "b", due="2026-11-21")
    env.post(f"/tasks/{a['id']}/start", headers=env.h("user"))
    clock.t += timedelta(seconds=30)
    env.post(f"/tasks/{b['id']}/start", headers=env.h("user"))
    s = snap(env)
    assert s["active"]["id"] == b["id"]
    ta = next(t for t in s["tasks"]["today"] if t["id"] == a["id"])
    assert ta["spent_seconds"] == 30 and not ta["running"]
    assert env.post(f"/tasks/{a['id']}/start", headers=env.h("viewer")).status_code == 403


def test_progress_reflects_completion(env):
    t = mk(env, "x", due="2026-11-21")
    env.post(f"/tasks/{t['id']}/complete", headers=env.h("user"))
    p = env.get("/progress", headers=env.h("viewer")).json()
    assert p["xp"] > 0 and p["total_done"] == 1 and p["today"]["done"] == 1


def test_plan_task_moves_planned_for_within_due(env):
    t = mk(env, "x", due="2026-11-25")
    ok = env.post(f"/tasks/{t['id']}/plan", json={"planned_for": "2026-11-23"}, headers=env.h("user"))
    assert ok.status_code == 200 and ok.json()["planned_for"] == "2026-11-23" and ok.json()["due"] == "2026-11-25"
    assert env.post(f"/tasks/{t['id']}/plan", json={"planned_for": "2026-11-26"}, headers=env.h("user")).status_code == 422   # depois do prazo
    assert env.post(f"/tasks/{t['id']}/plan", json={"planned_for": "2026-11-21"}, headers=env.h("user")).status_code == 422   # hoje não
    assert env.post(f"/tasks/{t['id']}/plan", json={"planned_for": "2026-11-23"}, headers=env.h("viewer")).status_code == 403


def test_today_tasks_carry_reschedule_options(env):
    t = mk(env, "x", due="2026-11-24", planned_for="2026-11-21")
    opts = next(x for x in snap(env)["tasks"]["today"] if x["id"] == t["id"])["options"]
    assert [o["day"] for o in opts] == ["2026-11-22", "2026-11-23", "2026-11-24"]
    late = mk(env, "atrasada", due="2026-11-18")
    assert next(x for x in snap(env)["tasks"]["today"] if x["id"] == late["id"])["options"] == []   # prazo fixo já passou
