import json
from datetime import timedelta


def mk(client, scope="user", **kw):
    body = {"title": "Estudar capítulo 3", **kw}
    return client.post("/tasks", json=body, headers=client.h(scope))


def events(client, type_=None):
    es = client.get("/events", headers=client.h("user")).json()
    return [e for e in es if type_ is None or e["type"] == type_]


# ---------- autenticação ----------
def test_requires_token(env):
    assert env.get("/tasks").status_code == 401
    assert env.get("/tasks", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert env.get("/health").status_code == 200


def test_viewer_cannot_write(env):
    assert mk(env, "viewer").status_code == 403
    assert env.get("/tasks", headers=env.h("viewer")).status_code == 200


# ---------- criação ----------
def test_create_and_list(env):
    r = mk(env, due="2026-11-25", tag="estudo", est_minutes=45)
    assert r.status_code == 201
    t = r.json()
    assert t["source"] == "manual" and t["kind"] == "manual" and t["status"] == "open"
    assert env.get("/tasks", headers=env.h("user")).json()[0]["id"] == t["id"]
    assert events(env, "task.created")


def test_title_validation(env):
    assert mk(env, title="   ").status_code == 422
    assert mk(env, title="Fazer #urgente").status_code == 422
    assert mk(env, title="Fazer @{2026-01-01}").status_code == 422
    assert mk(env, title="x" * 201).status_code == 422
    assert mk(env, due="2099-01-01").status_code == 422          # fora da faixa plausível
    assert mk(env, extra="campo").status_code == 422             # campos desconhecidos são recusados


def test_scope_kinds(env):
    assert mk(env, "user", kind="moodle").status_code == 403
    assert mk(env, "moodle", kind="manual").status_code == 403
    assert mk(env, "planner").status_code == 403                 # planner só propõe


# ---------- Moodle: idempotência ----------
def test_moodle_upsert(env):
    a = mk(env, "moodle", title="Trabalho 1", source_ref="assign-10", due="2026-11-30")
    assert a.status_code == 201 and a.json()["kind"] == "moodle" and a.json()["source"] == "moodle"
    b = mk(env, "moodle", title="Trabalho 1", source_ref="assign-10", due="2026-11-30")
    assert b.status_code == 200 and b.json()["id"] == a.json()["id"]
    assert len(env.get("/tasks", headers=env.h("user")).json()) == 1
    assert not events(env, "task.updated")                       # nada mudou: sem evento

    c = mk(env, "moodle", title="Trabalho 1 (revisado)", source_ref="assign-10", due="2026-12-02")
    assert c.status_code == 200 and c.json()["due"] == "2026-12-02"
    ev = events(env, "task.updated")
    assert len(ev) == 1 and ev[0]["payload"]["changed"]["due"]["to"] == "2026-12-02"


def test_source_ref_is_per_source(env):
    mk(env, "moodle", title="A", source_ref="1")
    assert mk(env, "user", title="B", source_ref="1").status_code == 201


def test_moodle_does_not_reopen_done(env):
    t = mk(env, "moodle", title="Trabalho", source_ref="a1", due="2026-11-30").json()
    env.post(f"/tasks/{t['id']}/complete", headers=env.h("user"))
    again = mk(env, "moodle", title="Trabalho", source_ref="a1", due="2026-12-05").json()
    assert again["status"] == "done" and again["due"] == "2026-11-30"


# ---------- concluir ----------
def test_only_user_completes(env):
    t = mk(env).json()
    assert env.post(f"/tasks/{t['id']}/complete", headers=env.h("moodle")).status_code == 403
    assert env.post(f"/tasks/{t['id']}/complete", headers=env.h("planner")).status_code == 403
    assert env.post(f"/tasks/{t['id']}/complete", headers=env.h("user")).status_code == 200


def test_complete_is_idempotent_and_emits_once(env):
    t = mk(env, due="2026-11-21", tag="estudo").json()
    for _ in range(3):
        r = env.post(f"/tasks/{t['id']}/complete", headers=env.h("user"))
        assert r.status_code == 200 and r.json()["status"] == "done"
    done = events(env, "task.completed")
    assert len(done) == 1
    p = done[0]["payload"]
    assert p["day"] == "2026-11-21" and p["late"] is False and p["tag"] == "estudo"


def test_late_flag_and_day_boundary(env, clock):
    t = mk(env, due="2026-11-20").json()
    env.post(f"/tasks/{t['id']}/complete", headers=env.h("user"))
    assert events(env, "task.completed")[0]["payload"]["late"] is True

    # 02:00 de 22/11 ainda é o "dia" 21/11 (o dia vira às 04:00)
    clock.t = clock.t.replace(day=22, hour=2)
    t2 = mk(env, title="Outra", due="2026-11-21").json()
    env.post(f"/tasks/{t2['id']}/complete", headers=env.h("user"))
    p = events(env, "task.completed")[1]["payload"]
    assert p["day"] == "2026-11-21" and p["late"] is False

    clock.t = clock.t.replace(hour=5)                            # depois das 04:00 já é 22/11
    t3 = mk(env, title="Terceira", due="2026-11-21").json()
    env.post(f"/tasks/{t3['id']}/complete", headers=env.h("user"))
    p = events(env, "task.completed")[2]["payload"]
    assert p["day"] == "2026-11-22" and p["late"] is True
    assert env.get("/summary", headers=env.h("user")).json()["done_today"] == 1


def test_complete_missing_and_dropped(env):
    assert env.post("/tasks/999/complete", headers=env.h("user")).status_code == 404


# ---------- propostas ----------
def test_reschedule_needs_approval(env):
    t = mk(env, due="2026-11-25").json()
    r = env.post(f"/tasks/{t['id']}/reschedule", json={"planned_for": "2026-11-23", "reason": "viagem"}, headers=env.h("user"))
    assert r.status_code == 202
    pid = r.json()["id"]
    assert env.get("/tasks", headers=env.h("user")).json()[0]["planned_for"] is None      # nada mudou ainda
    assert env.post(f"/proposals/{pid}/approve", headers=env.h("planner")).status_code == 403
    ok = env.post(f"/proposals/{pid}/approve", headers=env.h("user")).json()
    assert ok["status"] == "applied"
    assert env.get("/tasks", headers=env.h("user")).json()[0]["planned_for"] == "2026-11-23"
    assert env.post(f"/proposals/{pid}/approve", headers=env.h("user")).status_code == 409   # não decide duas vezes


def test_reject_leaves_task_untouched(env):
    t = mk(env, due="2026-11-25").json()
    pid = env.post(f"/tasks/{t['id']}/drop", json={}, headers=env.h("user")).json()["id"]
    assert env.post(f"/proposals/{pid}/reject", headers=env.h("user")).json()["status"] == "rejected"
    assert env.get("/tasks", headers=env.h("user")).json()[0]["status"] == "open"


def test_drop_after_approval(env):
    t = mk(env).json()
    pid = env.post(f"/tasks/{t['id']}/drop", json={"reason": "não faz mais sentido"}, headers=env.h("user")).json()["id"]
    env.post(f"/proposals/{pid}/approve", headers=env.h("user"))
    assert env.get("/tasks?status=dropped", headers=env.h("user")).json()[0]["id"] == t["id"]
    assert env.post(f"/tasks/{t['id']}/complete", headers=env.h("user")).status_code == 409


def test_stale_proposal(env):
    t = mk(env, "moodle", title="T", source_ref="x", due="2026-11-30").json()
    pid = env.post(f"/tasks/{t['id']}/reschedule", json={"planned_for": "2026-11-28"}, headers=env.h("user")).json()["id"]
    # clock avança e a origem altera a tarefa antes da aprovação
    env.app.state.clock.t += timedelta(minutes=5)
    mk(env, "moodle", title="T", source_ref="x", due="2026-12-01")
    r = env.post(f"/proposals/{pid}/approve", headers=env.h("user")).json()
    assert r["status"] == "stale"
    assert env.get("/tasks", headers=env.h("user")).json()[0]["planned_for"] is None


def test_moodle_due_is_not_changeable(env):
    t = mk(env, "moodle", title="T", source_ref="x", due="2026-11-30").json()
    assert env.post(f"/tasks/{t['id']}/reschedule", json={"due": "2026-12-20"}, headers=env.h("user")).status_code == 403
    assert env.post(f"/tasks/{t['id']}/reschedule", json={"planned_for": "2026-11-28"}, headers=env.h("user")).status_code == 202


def test_planner_proposes_creation(env):
    body = {"kind": "task.create", "task": {"title": "Revisar capítulo 3", "planned_for": "2026-11-23", "kind": "review", "est_minutes": 30},
            "reason": "revisão FSRS"}
    assert env.post("/proposals", json=body, headers=env.h("planner")).status_code == 201
    assert env.get("/tasks", headers=env.h("user")).json() == []               # ainda é só proposta
    # planner não pode criar tipo que não é dele
    bad = {"kind": "task.create", "task": {"title": "X", "kind": "manual"}}
    assert env.post("/proposals", json=bad, headers=env.h("planner")).status_code == 403
    pid = env.get("/proposals?status=pending", headers=env.h("user")).json()[0]["id"]
    env.post(f"/proposals/{pid}/approve", headers=env.h("user"))
    t = env.get("/tasks", headers=env.h("user")).json()[0]
    assert t["source"] == "planner" and t["kind"] == "review" and t["planned_for"] == "2026-11-23"


def test_viewer_cannot_propose(env):
    body = {"kind": "task.create", "task": {"title": "X"}}
    assert env.post("/proposals", json=body, headers=env.h("viewer")).status_code == 403


# ---------- projeção ----------
def test_projection_file(env):
    a = mk(env, title="Lista de exercícios", due="2026-11-25", tag="estudo").json()
    mk(env, title="Ler artigo")
    env.post(f"/tasks/{a['id']}/complete", headers=env.h("user"))
    text = env.settings.tasks_file.read_text(encoding="utf-8")
    assert "kanban-plugin: board" in text
    open_part, done_part = text.split("## Concluído")
    assert "- [ ] Ler artigo" in open_part
    assert "- [x] Lista de exercícios @{2026-11-25} #estudo" in done_part
    assert "**Complete**" in done_part and "%% kanban:settings" in text


def test_projection_hides_old_done(env, clock):
    a = mk(env, title="Velha").json()
    env.post(f"/tasks/{a['id']}/complete", headers=env.h("user"))
    clock.t += timedelta(days=30)
    mk(env, title="Nova")
    assert "Velha" not in env.settings.tasks_file.read_text(encoding="utf-8")


def test_projection_not_rewritten_when_same(env, clock):
    from studyagent import projection
    from studyagent.db import connect
    mk(env, title="A")
    conn = connect(env.settings.db_path)
    assert projection.write(conn, env.settings, clock()) is False     # já está em dia: não reescreve
    conn.close()


# ---------- eventos / resumo ----------
def test_events_after(env):
    mk(env, title="A"); mk(env, title="B")
    es = events(env)
    assert [e["type"] for e in es] == ["task.created", "task.created"]
    after = env.get(f"/events?after={es[0]['id']}", headers=env.h("user")).json()
    assert len(after) == 1


def test_summary(env, clock):
    mk(env, title="A", due="2026-11-21")
    mk(env, title="B", due="2026-11-10")
    s = env.get("/summary", headers=env.h("user")).json()
    assert s["open_total"] == 2 and s["open_due_today"] == 1 and s["overdue"] == 1 and s["today"] == "2026-11-21"


def test_projection_shows_planned_day(env):
    t = mk(env, "moodle", title="Trabalho", source_ref="a", due="2026-11-28").json()
    pid = env.post(f"/tasks/{t['id']}/reschedule", json={"planned_for": "2026-11-26"}, headers=env.h("user")).json()["id"]
    env.post(f"/proposals/{pid}/approve", headers=env.h("user"))
    assert "- [ ] Trabalho @{2026-11-26}" in env.settings.tasks_file.read_text(encoding="utf-8")


def test_projection_replaces_unreadable_file(env):
    import os
    p = env.settings.tasks_file
    mk(env, title="A")
    os.chmod(p, 0o000)                                 # arquivo ilegível, mas a pasta é nossa
    mk(env, title="B")
    os.chmod(p, 0o644) if p.exists() else None
    assert "- [ ] B" in p.read_text(encoding="utf-8")
