import json
from collections import Counter
from pathlib import Path

import pytest

from studyagent import schedule as sch
from studyagent import schedule_scan
from studyagent.schedule import Page, parse_tables, parse_with_llm, validate

FIX = Path(__file__).parent / "fixtures"


def pages(key):
    return [Page(p["text"], p["tables"]) for p in json.loads((FIX / f"{key}.json").read_text())]


def course(key):
    return validate(parse_tables(pages(key), "2026-2", key))


EXPECTED = {   # sessões lidas dos 6 cronogramas reais (semestre 2026-2)
    "CPP": {"class": 28, "empty": 8, "exam": 2},
    "ELE064": {"class": 12, "exam": 4},
    "MAT042": {"class": 24, "exam": 5, "no_class": 1},
    "FIS069": {"class": 26, "exam": 5, "holiday": 4, "no_class": 3},
    "FIS065": {"class": 18, "exam": 4},
    "DCC216": {"class": 27, "exam": 3},
}


# ---------- parser ----------
@pytest.mark.parametrize("key", list(EXPECTED))
def test_real_schedules(key):
    c = course(key)
    assert dict(Counter(s.kind for s in c.sessions)) == EXPECTED[key]
    assert c.warnings == []
    assert [s.date for s in c.sessions] == sorted(s.date for s in c.sessions)
    assert all(s.label.startswith(f"{key}: ") for s in c.sessions)


def test_header_info_and_year_and_time():
    c = course("ELE064")                                   # datas sem ano; horário só no cabeçalho
    assert (c.code, c.section) == ("ELE064", "TTM") and c.professor.startswith("Diógenes")
    assert c.sessions[0].date == "2026-08-14" and (c.sessions[0].start, c.sessions[0].end) == ("13:00", "14:40")
    assert course("MAT042").sessions[0].start == "09:25" and course("DCC216").name == "MATEMÁTICA DISCRETA"
    assert course("CPP").code is None                      # sem cabeçalho: a sigla da pasta vale


def test_empty_holiday_and_highlights():
    cpp = course("CPP")
    empties = [s for s in cpp.sessions if s.kind == "empty"]
    assert len(empties) == 8 and all(s.label == "CPP: vazio" and s.n is None for s in empties)
    hol = [s for s in course("FIS069").sessions if s.kind == "holiday"]
    assert any("7 Setembro" in s.label for s in hol)
    flagged = {s.flag for s in cpp.sessions if s.flag}
    assert flagged == {"duvidas,revisao"}                  # "Revisão e Dúvidas"
    assert {s.flag for s in course("FIS069").sessions if s.flag} == {"duvidas"}
    assert [s.kind for s in course("MAT042").sessions if "Entrega de notas" in s.title] == ["no_class"]


def test_mecanica_long_rows_and_headerless_table():
    c = course("FIS065")
    assert len(c.sessions) == 22 and c.sessions[0].n == 1   # as 4 primeiras linhas vêm numa tabela sem cabeçalho
    assert not any(s.title.lower().startswith("semana ") and s.kind == "class" and "de 2" in s.title[:12] for s in c.sessions)
    assert [s.kind for s in c.sessions if "Realização da Prova" in s.title] == ["exam"] * 3


def test_classify():
    assert sch.classify("")[0] == "empty"
    assert sch.classify("Prova!")[0] == "exam" and sch.classify("1a. Prova - 30 pontos")[0] == "exam"
    assert sch.classify("Introdução ao teste de software")[0] == "class"
    assert sch.classify("Resolução de provas anteriores")[0] == "class"      # só vira prova se a linha É a prova
    assert sch.classify("Feriado - 12 outubro - Não haverá aula")[0] == "holiday"
    assert sch.classify("Revisão e Dúvidas") == ("class", "duvidas,revisao")


def test_weekday_mismatch_and_gaps_warn():
    ps = pages("DCC216")
    for t in ps[0].tables:
        for r in t:
            if r and len(r) > 2 and r[0] == "2":
                r[2] = "Dom"                                # o PDF diz domingo, mas 13/08/2026 é quinta
            if r and len(r) > 2 and r[0] == "5":
                r[0] = "7"
    c = validate(parse_tables(ps, "2026-2", "DCC216"))
    assert any("Dom" in w for w in c.warnings) and any("numeração" in w or "sequência" in w for w in c.warnings)


def test_year_rollover_without_year():
    ps = [Page("", [[["Aula", "Data", "Conteúdo"], ["1", "10/12", "A"], ["2", "17/12", "B"], ["3", "07/01", "C"], ["4", "14/01", "D"], ["5", "21/01", "E"]]])]
    c = parse_tables(ps, "2026-2", "X")
    assert [s.date for s in c.sessions] == ["2026-12-10", "2026-12-17", "2027-01-07", "2027-01-14", "2027-01-21"]


# ---------- fallback com LLM ----------
class FakeLLM:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    def chat_json(self, system, user, max_tokens=0):
        self.calls += 1
        return self.answers.pop(0)


def test_llm_fallback_validates_output():
    ps = [Page("Cronograma\nAula 1 em 11 de agosto: Intro\nAula 2 em 13 de agosto: Prova", [])]
    llm = FakeLLM([{"sessions": [
        {"n": 1, "date": "2026-08-11", "title": "Intro", "start": "13:00", "end": "14:40"},
        {"n": 2, "date": "2026-08-13", "title": "Prova"},
        {"n": 3, "date": "2026-02-30", "title": "data impossível"},
        {"n": 4, "date": "2026-08-13", "title": "duplicada"}]}])
    c = parse_with_llm(ps, "2026-2", "Z", llm)
    assert [(s.date, s.kind) for s in c.sessions] == [("2026-08-11", "class"), ("2026-08-13", "exam")]
    assert c.method == "llm" and any("LLM" in w for w in c.warnings) and any("inválida" in w for w in c.warnings)


def test_llm_not_used_when_tables_work(monkeypatch):
    monkeypatch.setattr(sch, "read_pdf", lambda p: pages("DCC216"))
    llm = FakeLLM([])
    c = sch.parse_pdf(Path("x.pdf"), "2026-2", "DCC216", llm)
    assert llm.calls == 0 and c.method == "tables"
    monkeypatch.setattr(sch, "read_pdf", lambda p: [Page("nada útil", [])])
    with pytest.raises(ValueError):
        sch.parse_pdf(Path("x.pdf"), "2026-2", "DCC216", None)


def test_paths(tmp_path):
    f = tmp_path / "UFMG" / "2026-2" / "Matérias" / "DCC216" / "Cronograma.pdf"
    f.parent.mkdir(parents=True); f.write_bytes(b"x")
    assert sch.find_pdfs(tmp_path / "UFMG") == [("2026-2", "DCC216", f)]
    assert sch.semester_key_from_path(f) == ("2026-2", "DCC216")
    assert sch.semester_key_from_path(tmp_path / "x.pdf") is None


def test_study_due():
    assert sch.study_due("2026-09-22", "2026-08-11", 5, None, 1.0) == "2026-09-17"          # aula - 5 dias
    assert sch.study_due("2026-08-11", "2026-08-11", 5, "2026-12-05", 1.0) == "2026-12-05"  # 1ª aula no início escolhido
    assert sch.study_due("2026-08-25", "2026-08-11", 5, "2026-12-05", 1.0) == "2026-12-19"  # mesmo espaçamento
    assert sch.study_due("2026-08-25", "2026-08-11", 5, "2026-12-05", 0.5) == "2026-12-12"  # comprimido


# ---------- integração com o daemon ----------
def H(env, scope="user"):
    return env.h(scope)


def propose(env, key, scope="schedule", mutate=None, h="hash1"):
    p = course(key).payload(h)
    if mutate:
        mutate(p)
    return env.post("/proposals", json={"kind": "schedule.import", "schedule": p, "reason": "t"}, headers=H(env, scope))


def approve(env, pid):
    return env.post(f"/proposals/{pid}/approve", headers=H(env)).json()


def tasks(env, **params):
    return [t for t in env.get("/tasks", params=params, headers=H(env)).json() if t["source"] == "schedule"]


def test_import_flow(env):
    r = propose(env, "DCC216")
    assert r.status_code == 201
    assert env.get("/courses", headers=H(env)).json() == []              # nada entra antes da aprovação
    assert tasks(env) == []
    out = approve(env, r.json()["id"])
    assert out["status"] == "applied"
    c = env.get("/courses", headers=H(env)).json()
    assert len(c) == 1 and (c[0]["key"], c[0]["semester"], c[0]["source_hash"]) == ("DCC216", "2026-2", "hash1")
    ts = tasks(env, status="open")
    assert len(ts) == 27 and all(t["kind"] == "study" and t["course"] == "DCC216" for t in ts)   # todas as aulas viram tarefa
    t1 = next(t for t in ts if t["source_ref"] == "2026-2:DCC216:12")
    assert t1["due"] == "2026-09-17" and t1["title"].startswith("Estudar: Comportamento assintótico")   # aula 22/09 - 5 dias
    cal = env.get("/calendar", params={"course": "DCC216"}, headers=H(env)).json()
    assert len(cal) == 30 and sum(1 for s in cal if s["kind"] == "exam") == 3
    assert any(s["highlight"] and s["flag"] == "revisao" for s in cal)     # revisão destacada
    assert not [s for s in cal if s["kind"] == "exam" and s["task_id"]]    # prova não gera tarefa
    assert any(p["kind"] == "plan.apply" for p in env.get("/proposals", params={"status": "pending"}, headers=H(env)).json())


def test_empty_events_in_calendar(env):
    approve(env, propose(env, "CPP").json()["id"])
    cal = env.get("/calendar", params={"course": "CPP"}, headers=H(env)).json()
    assert sum(1 for s in cal if s["label"] == "CPP: vazio") == 8 and len(tasks(env)) == 28
    assert env.get("/calendar", params={"from": "2026-09-20", "to": "2026-09-30"}, headers=H(env)).json()[0]["date"] >= "2026-09-20"


def test_reimport_is_idempotent_and_updates(env):
    approve(env, propose(env, "DCC216").json()["id"])
    n_before = len(env.get("/tasks", headers=H(env)).json())
    approve(env, propose(env, "DCC216", h="hash2").json()["id"])
    assert len(env.get("/tasks", headers=H(env)).json()) == n_before     # mesmas tarefas
    done = next(t for t in tasks(env, status="open") if t["source_ref"].endswith(":1"))["id"]
    env.post(f"/tasks/{done}/complete", headers=H(env))

    def edit(p):                                                          # professor mudou a aula 3 de dia e apagou a 27
        for s in p["sessions"]:
            if s["n"] == 3:
                s["date"] = "2026-08-19"
        p["sessions"] = [s for s in p["sessions"] if s["n"] != 27]
    approve(env, propose(env, "DCC216", h="hash3", mutate=edit).json()["id"])
    open_ = {t["source_ref"]: t for t in tasks(env, status="open")}
    assert open_["2026-2:DCC216:3"]["due"] == "2026-08-14"               # 19/08 - 5
    assert "2026-2:DCC216:27" not in open_
    assert any(t["status"] == "dropped" and t["source_ref"].endswith(":27") for t in tasks(env, status="dropped"))
    assert len(tasks(env, status="done")) == 1                            # concluída não é mexida


def test_pending_import_superseded_and_permissions(env):
    a = propose(env, "DCC216", h="a").json()["id"]
    b = propose(env, "DCC216", h="b").json()["id"]
    pend = [p for p in env.get("/proposals", params={"status": "pending"}, headers=H(env)).json() if p["kind"] == "schedule.import"]
    assert [p["id"] for p in pend] == [b]
    assert env.post(f"/proposals/{b}/approve", headers=H(env, "schedule")).status_code == 403
    assert env.post("/schedule/shift", json={"semester": "2026-2"}, headers=H(env, "schedule")).status_code == 403
    assert propose(env, "DCC216", scope="viewer").status_code == 403
    assert propose(env, "DCC216", mutate=lambda p: p.update(semester="26-2")).status_code == 422
    assert propose(env, "DCC216", mutate=lambda p: p.update(key="../x")).status_code == 422
    assert propose(env, "DCC216", mutate=lambda p: p["sessions"][0].update(flag="x")).status_code == 422
    assert propose(env, "DCC216", mutate=lambda p: p.update(sessions=[])).status_code == 422
    assert env.get("/proposals/%d" % a, headers=H(env)) is not None


def test_shift_adapts_study_deadlines(env):
    approve(env, propose(env, "DCC216").json()["id"])
    done_id = next(t for t in tasks(env, status="open") if t["source_ref"].endswith(":1"))["id"]
    env.post(f"/tasks/{done_id}/complete", headers=H(env))
    r = env.post("/schedule/shift", json={"semester": "2026-2", "start": "2026-12-05"}, headers=H(env))
    assert r.status_code == 200 and r.json()["courses"][0]["tasks_moved"] == 26      # a concluída não muda
    by_ref = {t["source_ref"]: t for t in tasks(env)}
    assert by_ref["2026-2:DCC216:2"]["due"] == "2026-12-07"                           # aula 2 = 1ª aula + 2 dias
    assert by_ref["2026-2:DCC216:29"]["due"] == "2027-03-15"                          # 19/11 = 1ª aula + 100 dias
    env.post("/schedule/shift", json={"semester": "2026-2", "k": 0.5}, headers=H(env))
    assert {t["source_ref"]: t for t in tasks(env)}["2026-2:DCC216:2"]["due"] == "2026-12-06"
    env.post("/schedule/shift", json={"semester": "2026-2", "clear": True}, headers=H(env))
    assert {t["source_ref"]: t for t in tasks(env)}["2026-2:DCC216:2"]["due"] == "2026-08-08"    # 13/08 - 5
    assert env.post("/schedule/shift", json={"semester": "2099-1"}, headers=H(env)).status_code == 404
    assert env.post("/schedule/shift", json={"semester": "2026-2", "k": 9}, headers=H(env)).status_code == 422


# ---------- vigia de pasta ----------
def test_scan_skips_known_hashes(env, tmp_path, monkeypatch):
    root = tmp_path / "UFMG"
    for k, content in (("DCC216", b"a"), ("MAT042", b"b")):
        d = root / "2026-2" / "Matérias" / k
        d.mkdir(parents=True); (d / "Cronograma.pdf").write_bytes(content)
    monkeypatch.setattr(schedule_scan, "parse_pdf", lambda path, sem, key, llm=None: course(key))
    env.headers.update(env.h("schedule"))
    res = schedule_scan.scan(env, root)
    assert [r["key"] for r in res] == ["DCC216", "MAT042"] and all("proposal" in r for r in res)
    assert schedule_scan.scan(env, root) == []                          # já há proposta pendente com esse hash
    env.headers.update(env.h("user"))
    for p in env.get("/proposals", params={"status": "pending"}).json():
        if p["kind"] == "schedule.import":
            env.post(f"/proposals/{p['id']}/approve")
    env.headers.update(env.h("schedule"))
    assert schedule_scan.scan(env, root) == []                          # já importado
    (root / "2026-2" / "Matérias" / "DCC216" / "Cronograma.pdf").write_bytes(b"alterado")
    assert [r["key"] for r in schedule_scan.scan(env, root)] == ["DCC216"]


def test_scan_survives_bad_pdf(env, tmp_path, monkeypatch):
    d = tmp_path / "UFMG" / "2026-2" / "Matérias" / "BAD"
    d.mkdir(parents=True); (d / "Cronograma.pdf").write_bytes(b"lixo")
    env.headers.update(env.h("schedule"))
    failed: set[str] = set()
    res = schedule_scan.scan(env, tmp_path / "UFMG", None, failed)
    assert "error" in res[0] and failed
    assert schedule_scan.scan(env, tmp_path / "UFMG", None, failed) == []   # não repete o PDF ruim a cada varredura


def test_snapshot_with_courses(env):
    approve(env, propose(env, "DCC216").json()["id"])
    approve(env, propose(env, "CPP", h="h2").json()["id"])
    s = env.get("/snapshot", params={"before": 120, "after": 200}, headers=H(env, "viewer")).json()
    assert {c["key"] for c in s["courses"]} == {"DCC216", "CPP"} and sorted(c["idx"] for c in s["courses"]) == [0, 1]
    assert s["calendar"] and all("highlight" in x for x in s["calendar"])
    t = next(t for b in s["tasks"].values() for t in b)
    assert t["course_idx"] in (0, 1) and t["course_name"] and t["delivery"] is False
    assert s["task_dots"] and all("course_idx" in d for v in s["task_dots"].values() for d in v)
    import json as _j
    _j.dumps(s)
