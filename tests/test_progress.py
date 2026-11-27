from datetime import date, timedelta

from studyagent.progress import Completion as C, DaySnap as S, ProgressConfig, compute, level_of, xp_for_level

CFG = ProgressConfig()


def ok_day(day, first_id, n=4, programmed=30, scheduled=(), late=False):
    comps = [C(first_id + i, day, late and i == 0) for i in range(n)]
    return comps, S(day, tuple(scheduled), programmed)


def run(days, today, cfg=CFG):
    comps, snaps = [], []
    for d in days:
        c, s = d
        comps += c; snaps.append(s)
    return compute(comps, snaps, today, cfg)


def test_empty():
    r = compute([], [], "2026-11-26")
    assert (r["xp"], r["level"], r["streak"], r["freezes"], r["total_done"]) == (0, 0, 0, 0, 0)
    assert r["today"]["on_track"] is False


def test_levels():
    assert xp_for_level(1, CFG) == 50 and xp_for_level(4, CFG) == 400
    assert level_of(0, CFG) == 0 and level_of(49, CFG) == 0 and level_of(50, CFG) == 1 and level_of(141, CFG) == 2


def test_streak_grows_and_multiplier_applies_next_day():
    days = [ok_day(f"2026-11-{d:02d}", d * 10) for d in (1, 2, 3)]
    r = run(days, "2026-11-04")
    assert r["streak"] == 3 and r["best_streak"] == 3
    # dia 1: 4×10; dia 2 (streak 1, ×1.005): 4×round(10.05)=40; dia 3 (streak 2, ×1.01): 4×10 -> 40; + 3 bônus de 20
    assert r["xp"] == 40 * 3 + 60 and r["total_done"] == 12
    assert [h["status"] for h in r["history"]] == ["ok"] * 3


def test_multiplier_cap_and_rounding():
    comps, snaps = [], []
    for i in range(70):                                   # 70 dias seguidos
        d = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
        c, s = ok_day(d, 1000 + i * 10, 4)
        comps += c; snaps.append(s)
    r = compute(comps, snaps, (date(2026, 1, 1) + timedelta(days=70)).isoformat())
    assert r["streak"] == 70 and r["multiplier"] == 1.3     # teto: 60 dias × 0,5%


def test_late_task_pays_30_percent_and_breaks_day():
    comps, snap = ok_day("2026-11-01", 1, late=True)
    r = compute(comps, [snap], "2026-11-02")
    assert r["xp"] == 3 + 10 * 3                            # 1 atrasada (3 xp) + 3 no prazo, sem bônus
    assert r["history"][0]["status"] == "failed" and "late" in r["history"][0]["reasons"] and r["streak"] == 0


def test_min_scheduled_and_programmed_rules():
    r = run([ok_day("2026-11-01", 1, n=3)], "2026-11-02")
    assert r["history"][0]["reasons"] == ["min"]
    c, s = ok_day("2026-11-01", 1, n=4, scheduled=(1, 2, 99))   # a 99 nunca foi feita
    assert compute(c, [s], "2026-11-02")["history"][0]["reasons"] == ["scheduled"]
    c, s = ok_day("2026-11-01", 1, n=4, programmed=23)
    assert compute(c, [s], "2026-11-02")["history"][0]["reasons"] == ["programmed"]
    c, s = ok_day("2026-11-01", 1, n=4, scheduled=(1, 2, 3, 4))
    assert compute(c, [s], "2026-11-02")["history"][0]["status"] == "ok"


def test_scheduled_done_next_day_does_not_count_for_the_scheduled_day():
    comps = [C(1, "2026-11-02"), C(2, "2026-11-01"), C(3, "2026-11-01"), C(4, "2026-11-01"), C(5, "2026-11-01")]
    r = compute(comps, [S("2026-11-01", (1, 2), 30)], "2026-11-03")
    assert r["history"][0]["reasons"] == ["scheduled"]                  # a 1 só foi feita no dia seguinte


def test_dayoff_needs_only_two():
    comps = [C(1, "2026-11-01"), C(2, "2026-11-01")]
    r = compute(comps, [S("2026-11-01", (), 30, True)], "2026-11-02")
    assert r["history"][0]["status"] == "ok" and r["history"][0]["need"] == 2


def test_missing_day_breaks_streak_and_freeze_saves():
    # 60 tarefas feitas (15 dias de 4) rendem 1 freeze; o dia seguinte falha e o freeze cobre
    days = [ok_day(f"2026-11-{d:02d}", d * 10) for d in range(1, 16)]
    days.append(([], S("2026-11-16", (), 30)))                              # dia vazio
    days.append(ok_day("2026-11-17", 500))
    r = run(days, "2026-11-18")
    st = [h["status"] for h in r["history"]]
    assert st[:15] == ["ok"] * 15 and st[15] == "frozen" and st[16] == "ok"
    assert r["streak"] == 16 and r["freezes"] == 0 and r["best_streak"] == 16
    # sem freeze a streak zera
    r2 = run([ok_day("2026-11-01", 1), ([], S("2026-11-02", (), 30))], "2026-11-03")
    assert r2["streak"] == 0 and r2["history"][1]["status"] == "failed" and r2["best_streak"] == 1


def test_days_without_any_data_between_are_failures():
    comps, snap = ok_day("2026-11-01", 1)
    r = compute(comps, [snap], "2026-11-04")                                # dias 2 e 3 sem nada
    assert [h["status"] for h in r["history"]] == ["ok", "failed", "failed"] and r["streak"] == 0


def test_freeze_cap():
    days = [ok_day((date(2026, 1, 1) + timedelta(days=i)).isoformat(), 1000 + i * 10, 8) for i in range(40)]   # 320 tarefas = 5 freezes ganhos
    r = run(days, (date(2026, 1, 1) + timedelta(days=40)).isoformat())
    assert r["freezes"] == 3 and r["freeze_cap"] == 3


def test_today_is_open_xp_counts_but_streak_waits():
    comps, snap = ok_day("2026-11-05", 1, n=4, scheduled=(1, 2, 3, 4))
    r = compute(comps, [snap], "2026-11-05")
    assert r["xp"] == 40 and r["streak"] == 0 and r["today"]["on_track"] is True and r["today"]["scheduled_done"] == 4
    r = compute(comps[:3], [snap], "2026-11-05")
    assert r["today"]["on_track"] is False and r["week_done"] == 3


def test_week_window_and_dedupe():
    comps = [C(1, "2026-11-01"), C(2, "2026-11-05"), C(2, "2026-11-06"), C(3, "2026-11-07")]
    r = compute(comps, [], "2026-11-07")
    assert r["total_done"] == 3 and r["week_done"] == 3                     # janela 01..07; a 2 só conta uma vez
    assert compute(comps, [], "2026-11-08")["week_done"] == 2               # 01/11 saiu da janela
